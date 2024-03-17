# https://github.com/ultralytics/ultralytics/issues/1429#issuecomment-1519239409
# write by leichaokai
# 正式版本✈✈✈
from pathlib import Path
import torch
import argparse
import numpy as np
import cv2

import math
from types import SimpleNamespace

from boxmot.tracker_zoo import create_tracker
from boxmot.utils import ROOT, WEIGHTS
from boxmot.utils.checks import TestRequirements
from boxmot.utils import logger as LOGGER
from boxmot.utils.torch_utils import select_device

tr = TestRequirements()
tr.check_packages(('ultralytics',))  # install

from ultralytics.yolo.engine.model import YOLO, TASK_MAP
from ultralytics.yolo.utils import SETTINGS, colorstr, ops, is_git_dir, IterableSimpleNamespace
from ultralytics.yolo.utils.checks import check_imgsz, print_args
from ultralytics.yolo.utils.files import increment_path
#from ultralytics.yolo.engine.results import Boxes

from ultralytics.yolo.data.utils import VID_FORMATS

from multi_yolo_backend import MultiYolo
from utils import write_MOT_results
from strategy import tlbr_midpoint, intersect, ccw, vector_angle, vector_position, time_synchronized, get_size_with_pil,compute_color_for_labels


from collections import Counter
from collections import deque

def on_predict_start(predictor):
    predictor.trackers = []
    predictor.tracker_outputs = [None] * predictor.dataset.bs  # 后面一个参数是列表的长度
    predictor.args.tracking_config = \
        ROOT / \
        'boxmot' / \
        opt.tracking_method / \
        'configs' / \
        (opt.tracking_method + '.yaml')
    for i in range(predictor.dataset.bs):
        tracker = create_tracker(
            predictor.args.tracking_method,
            predictor.args.tracking_config,
            predictor.args.reid_model,
            predictor.args.device,
            predictor.args.half
        )
        predictor.trackers.append(tracker)


@torch.no_grad()
def run(args):
    model = YOLO(args['yolo_model'] if 'v8' in str(args['yolo_model']) else 'yolov8n')
    overrides = model.overrides.copy()
    model.predictor = TASK_MAP[model.task][3](overrides=overrides, _callbacks=model.callbacks)

    # extract task predictor提取任务的预测器
    predictor = model.predictor

    # combine default predictor args with custom, preferring custom
    combined_args = {**predictor.args.__dict__, **args}
    # overwrite default args
    predictor.args = IterableSimpleNamespace(**combined_args)
    predictor.args.device = select_device(args['device'])
    LOGGER.info(args)

    # setup source and model设置数据来源和模型
    if not predictor.model:
        predictor.setup_model(model=model.model, verbose=False)
    predictor.setup_source(predictor.args.source)

    predictor.args.imgsz = check_imgsz(predictor.args.imgsz, stride=model.model.stride, min_dim=2)  # check image size
    predictor.save_dir = increment_path(Path(predictor.args.project) / predictor.args.name,
                                        exist_ok=predictor.args.exist_ok)

    # Check if save_dir/ label file exists
    if predictor.args.save or predictor.args.save_txt:
        (predictor.save_dir / 'labels' if predictor.args.save_txt else predictor.save_dir).mkdir(parents=True,
                                                                                                 exist_ok=True)
    # Warmup model
    if not predictor.done_warmup:
        predictor.model.warmup(
            imgsz=(1 if predictor.model.pt or predictor.model.triton else predictor.dataset.bs, 3, *predictor.imgsz))
        predictor.done_warmup = True
    predictor.seen, predictor.windows, predictor.batch, predictor.profilers = 0, [], None, (
    ops.Profile(), ops.Profile(), ops.Profile(), ops.Profile())
    predictor.add_callback('on_predict_start', on_predict_start)
    predictor.run_callbacks('on_predict_start')
    model = MultiYolo(
        model=model.predictor.model if 'v8' in str(args['yolo_model']) else args['yolo_model'],
        device=predictor.device,
        args=predictor.args
    )

    #引入全新变量
    idx_frame = 0
    results = []
    paths = {}
    track_cls = 0
    last_track_id = -1
    total_track = 0
    angle = -1
    total_counter = 0
    out_count = 0  # 外出计数
    in_count = 0  # 进入计数
    class_counter = Counter()  # 存储每个检测类别的数量
    already_counted = deque(maxlen=30)  # 短期内储存已计数的id，deque储存可迭代的对象接口
    total_time_1 = 0
    in_count_id = deque(maxlen=80)
    tracking_num_output = 0
    fps = 0

    for frame_idx, batch in enumerate(predictor.dataset):
        idx_frame += 1
        fps += 1
        predictor.run_callbacks('on_predict_batch_start')
        predictor.batch = batch
        t1 = time_synchronized()
        path, im0s, vid_cap, s = batch
        # visualize = increment_path(save_dir / Path(path[0]).stem, exist_ok=True, mkdir=True) if predictor.args.visualize and (not predictor.dataset.source_type.tensor) else False

        n = len(im0s)
        predictor.results = [None] * n

        # Preprocess
        with predictor.profilers[0]:
            im = predictor.preprocess(im0s)

        # Inference
        with predictor.profilers[1]:
            preds = model(im, im0s)

        # Postprocess moved to MultiYolo
        with predictor.profilers[2]:
            predictor.results = model.postprocess(path, preds, im, im0s, predictor)
        predictor.run_callbacks('on_predict_postprocess_end')
        track_bees = predictor.tracker_outputs[0]


        # Visualize, save, write results 逐帧输出结果
        n = len(im0s)
        for i in range(n):

            if predictor.dataset.source_type.tensor:  # skip write, show and plot operations if input is raw tensor
                continue
            p, im0 = path[i], im0s[i].copy()
            p = Path(p)

            with predictor.profilers[3]:
                # get raw bboxes tensor
                dets = predictor.results[i].boxes.data
                # get tracker predictions
                # 在此处添加碰撞模块
                predictor.tracker_outputs[i] = predictor.trackers[i].update(dets.cpu().detach().numpy(), im0)
                track_bees = predictor.tracker_outputs[0]

            predictor.results[i].speed = {
                'preprocess': predictor.profilers[0].dt * 1E3 / n,
                'inference': predictor.profilers[1].dt * 1E3 / n,
                'postprocess': predictor.profilers[2].dt * 1E3 / n,
                'tracking': predictor.profilers[3].dt * 1E3 / n
            }

            # filter boxes masks and pose results by tracking results
            model.filter_results(i, predictor)
            # overwrite bbox results with tracker predictions
            #  model.overwrite_results(i, im0.shape[:2], predictor)
            model.overwrite_results(i, im0.shape[:2], predictor)

            # write inference results to a file or directory
            if predictor.args.verbose or predictor.args.save or predictor.args.save_txt or predictor.args.show:
                s += predictor.write_results(i, predictor.results, (p, im, im0))
                predictor.txt_path = Path(predictor.txt_path)

                # # write MOT specific results
                # if predictor.args.source.endswith(VID_FORMATS):
                #     predictor.MOT_txt_path = predictor.txt_path.parent / p.stem
                # else:
                #     # append folder name containing current img
                #     predictor.MOT_txt_path = predictor.txt_path.parent / p.parent.name

                if predictor.tracker_outputs[i].size != 0 and predictor.args.save_txt:
                    write_MOT_results(
                        predictor.MOT_txt_path,
                        predictor.results[i],
                        frame_idx,
                        i,
                    )
            #设置检测区域(测试框线)
            # line_1 = predictor.line_set(0.59, 0.3, 1.25, 0.3)
            # line_2 = predictor.line_set(1.25, 0.343, 0.59, 0.343)
            # line_3 = predictor.line_set(0.59, 0.343, 0.59, 0.3)
            # line_4 = predictor.line_set(1.25, 0.3, 1.25, 0.343)
            # line_5 = predictor.line_set(0.59, 0.5, 1.25, 0.5)
            # 57号
            line_1 = predictor.line_set(0.58, 0.318, 1.24, 0.318)
            line_2 = predictor.line_set(1.24, 0.355, 0.58, 0.355)
            line_3 = predictor.line_set(0.58, 0.355, 0.58, 0.318)
            line_4 = predictor.line_set(1.24, 0.318, 1.24, 0.355)
            line_5 = predictor.line_set(0.59, 0.5, 1.25, 0.5)
            # 58号
            # line_1 = predictor.line_set(0.8, 0.338, 1.46, 0.338)
            # line_2 = predictor.line_set(1.46, 0.385, 0.8, 0.385)
            # line_3 = predictor.line_set(0.8, 0.385, 0.8, 0.338)
            # line_4 = predictor.line_set(1.46, 0.338, 1.46, 0.385)
            # line_5 = predictor.line_set(0.59, 0.5, 1.25, 0.5)

            predictor.line_show(line_1, 255, 0, 255, 2)
            predictor.line_show(line_2, 255, 0, 255, 2)
            predictor.line_show(line_3, 255, 0, 255, 2)
            predictor.line_show(line_4, 255, 0, 255, 2)
            # predictor.line_show(line_5, 255, 0, 255, 7)

            # 添加过线器件
            for track in track_bees:
                bbox = track[0:4]
                track_id = int(track[-3])
                midpoint = tlbr_midpoint(bbox)
                tracking_num_output += 1
                if track_id not in paths:
                    paths[track_id] = deque(maxlen=2)
                    total_track = track_id


                paths[track_id].append(midpoint)
                previous_midpoint = paths[track_id][0]

                # 判断1线进入线进入情况
                if intersect(midpoint, previous_midpoint, line_1[0], line_1[1]) and track_id not in already_counted:
                    class_counter[track_cls] += 1
                    total_counter += 1
                    last_track_id = track_id
                    # 经过线跳红
                    predictor.line_show(line_1, 255, 0, 0, 2)  # 通过线闪动且变粗变红
                    already_counted.append(track_id)  # 将已经触碰的蜜蜂id记录

                    angle = vector_angle(midpoint, previous_midpoint)  # 计算角度
                    if angle < 0:
                        out_count += 1
                        already_counted.remove(track_id)
                    if angle > 0:
                        in_count += 1
                        in_count_id.append(track_id)
                    continue

                # 判断2线进入
                elif intersect(midpoint, previous_midpoint, line_2[0], line_2[1]) and track_id not in already_counted:
                    class_counter[track_cls] += 1
                    total_counter += 1
                    last_track_id = track_id;
                    predictor.line_show(line_2, 255, 0, 0, 2)

                    already_counted.append(track_id)  # Set already counted for ID to true.

                    angle = vector_angle(midpoint, previous_midpoint)

                    if angle > 0:
                        out_count += 1
                        already_counted.remove(track_id)
                    if angle < 0:
                        in_count += 1
                        in_count_id.append(track_id)
                    continue

                # 判断3线进入
                elif intersect(midpoint, previous_midpoint, line_3[0],
                               line_3[1]) and track_id not in already_counted:
                    class_counter[track_cls] += 1
                    total_counter += 1
                    last_track_id = track_id
                    predictor.line_show(line_3, 255, 0, 0, 2)

                    already_counted.append(track_id)  # Set already counted for ID to true.

                    way = vector_position(midpoint[0], previous_midpoint[0])
                    if way < 0:
                        out_count += 1
                        already_counted.remove(track_id)
                    else:
                        in_count += 1
                        in_count_id.append(track_id)
                    continue

                # 判断4线进入
                elif intersect(midpoint, previous_midpoint, line_4[0],
                               line_4[1]) and track_id not in already_counted:
                    class_counter[track_cls] += 1
                    total_counter += 1
                    last_track_id = track_id;
                    predictor.line_show(line_4, 255, 0, 0, 2)

                    already_counted.append(track_id)  # Set already counted for ID to true.

                    way = vector_position(midpoint[0], previous_midpoint[0])

                    if way > 0:
                        out_count += 1
                        already_counted.remove(track_id)
                    else:
                        in_count += 1
                        in_count_id.append(track_id)
                    continue


                # 判断是否在1处徘徊
                elif intersect(midpoint, previous_midpoint, line_1[0], line_1[1]) and track_id in in_count_id:

                    class_counter[track_cls] -= 1
                    total_counter -= 1

                    predictor.line_show(line_1, 255, 0, 0, 2)

                    angle_2 = vector_angle(midpoint, previous_midpoint)
                    if angle_2 < 0:
                        in_count -= 1
                        already_counted.remove(track_id)
                        in_count_id.remove(track_id)

                    continue

                # 判断是否在2处徘徊
                elif intersect(midpoint, previous_midpoint, line_2[0], line_2[1]) and track_id in in_count_id:

                    class_counter[track_cls] -= 1
                    total_counter -= 1

                    predictor.line_show(line_2, 255, 0, 0, 2)

                    angle_2 = vector_angle(midpoint, previous_midpoint)
                    if angle_2 > 0:
                        in_count -= 1
                        already_counted.remove(track_id)
                        in_count_id.remove(track_id)
                    continue

                # 判断是否在3处徘徊
                elif intersect(midpoint, previous_midpoint, line_3[0], line_3[1]) and track_id in in_count_id:

                    class_counter[track_cls] -= 1
                    total_counter -= 1

                    predictor.line_show(line_3, 255, 0, 0, 2)
                    way = vector_position(midpoint[0], previous_midpoint[0])
                    if way < 0:
                        in_count -= 1
                        already_counted.remove(track_id)
                        in_count_id.remove(track_id)
                    continue


                # 判断是否在4处徘徊
                elif intersect(midpoint, previous_midpoint, line_4[0], line_4[1]) and track_id in in_count_id:

                    class_counter[track_cls] -= 1
                    total_counter -= 1

                    predictor.line_show(line_4, 255, 0, 0, 2)
                    way = vector_position(midpoint[0], previous_midpoint[0])
                    if way > 0:
                        in_count -= 1
                        already_counted.remove(track_id)
                        in_count_id.remove(track_id)
                    continue

                if len(paths) > 50:
                    del paths[list(paths)[0]]

            font = {'family': 'Times New Roman',
                    'color': 'darkred',
                    'weight': 'normal',
                    'size': 16,
                    }

            # 实时输出界面
            # label = "total_track:{}".format(str(total_track), fontdict=font)
            # predictor.put_text_to_video(label, (100, 150))
            label_in = "Incoming:{}".format(str(in_count), fontdict=font)
            predictor.put_text_to_video(label_in, (100, 200))
            label_out = "Outgoing:{}".format(str(out_count), fontdict=font)
            predictor.put_text_to_video(label_out, (100, 250))
            # if last_track_id >= 0:


            # display an image in a window using OpenCV imshow()
            if predictor.args.show and predictor.plotted_img is not None:
                predictor.show(p.parent)

                # predictor.line_show(predictor.line_set(0.59, 0.3, 1.25, 0.3))

            # save video predictions
            if predictor.args.save and predictor.plotted_img is not None:
                predictor.save_preds(vid_cap, i, str(predictor.save_dir / p.name))

        predictor.run_callbacks('on_predict_batch_end')

        # print time (inference-only)
        if predictor.args.verbose:
            LOGGER.info(
                f'{s}YOLO {predictor.profilers[1].dt * 1E3:.1f}ms, TRACKING {predictor.profilers[3].dt * 1E3:.1f}ms')

        # Release assets
        if isinstance(predictor.vid_writer[-1], cv2.VideoWriter):
            predictor.vid_writer[-1].release()  # release final video writer

        # Print results
        # if predictor.args.verbose and predictor.seen:
        #     t = tuple(x.t / predictor.seen * 1E3 for x in predictor.profilers)  # speeds per image
        #     LOGGER.info(
        #         f'Speed: %.1fms preprocess, %.1fms inference, %.1fms postprocess, %.1fms tracking per image at shape '
        #         f'{(1, 3, *predictor.args.imgsz)}' % t)
        # if predictor.args.save or predictor.args.save_txt or predictor.args.save_crop:
        #     nl = len(list(predictor.save_dir.glob('labels/*.txt')))  # number of labels
        #     s = f"\n{nl} label{'s' * (nl > 1)} saved to {predictor.save_dir / 'labels'}" if predictor.args.save_txt else ''
        #     LOGGER.info(f"Results saved to {colorstr('bold', predictor.save_dir)}{s}")

        # 时间计算
        predictor.run_callbacks('on_predict_end')
        end = time_synchronized()

        total_time_1 = total_time_1 + end - t1
        # if predictor.args.result:
        #   predictor.filter_result()
    # predictor.results[i].speed(predictor.profilers[0].dt + predictor.profilers[1].dt + predictor.profilers[2].dt + predictor.profilers[3].dt)
    print("进出蜜蜂总数：{}".format(str(total_counter)), ","
          "识别总用时：{}s".format(str(total_time_1)), ","
          "进入蜂箱的蜜蜂总数：{}只".format(str(in_count)), ","
          "离开蜂箱的蜜蜂总数：{}只".format(str(out_count)),","
          "每帧平均跟踪的蜜蜂数目：{}".format((tracking_num_output/idx_frame)), ","
          "平均帧率：{}".format(str(fps/total_time_1)))


def parse_opt():
    parser = argparse.ArgumentParser()
    parser.add_argument('--yolo-model', type=Path, default=WEIGHTS / 'v8m_250.pt', help='model.pt path(s)')
    parser.add_argument('--reid-model', type=Path, default=WEIGHTS / 'mobilenetv2_x1_4_dukemtmcreid.pt')
    parser.add_argument('--tracking-method', type=str, default='ocsort',
                        help='deepocsort, botsort, strongsort, ocsort, bytetrack')
    # 跟踪器：sort deepsort botsort bytetrack strongsort
    parser.add_argument('--source', type=str, default='video/show/05-01-08.mp4', help='file/dir/URL/glob, 0 for webcam')
    parser.add_argument('--imgsz', '--img', '--img-size', nargs='+', type=int, default=[640], help='inference size h,w')
    parser.add_argument('--conf', type=float, default=0.68, help='confidence threshold')
    parser.add_argument('--iou', type=float, default=0.7, help='intersection over union (IoU) threshold for NMS')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--show', action='store_false', help='display tracking video results')
    parser.add_argument('--save', action='store_true', help='save video tracking results')
    # # class 0 is person, 1 is bycicle, 2 is car... 79 is oven
    parser.add_argument('--classes', nargs='+', type=int, help='filter by class: --classes 0, or --classes 0 2 3')
    parser.add_argument('--project', default=ROOT / 'runs' / 'track', help='save results to project/name')
    parser.add_argument('--name', default='exp', help='save results to project/name')
    parser.add_argument('--exist-ok', action='store_true', help='existing project/name ok, do not increment')
    parser.add_argument('--half', action='store_true', help='use FP16 half-precision inference')
    parser.add_argument('--vid-stride', type=int, default=1, help='video frame-rate stride')
    parser.add_argument('--hide-label', action='store_true', help='hide labels when show')
    parser.add_argument('--hide-conf', action='store_true', help='hide confidences when show')
    parser.add_argument('--save-txt', action='store_true', help='save tracking results in a txt file')

    # parser.add_argument('--result', action='store_false', help='show the results?')
    opt = parser.parse_args()
    return opt


def main(opt):
    run(vars(opt))


if __name__ == "__main__":
    opt = parse_opt()
    main(opt)