# Copyright (c) 2017-present, Facebook, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##############################################################################
"""Test a Detectron network on an imdb (image database)."""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import datetime
import logging
import os
from collections import defaultdict

import cv2
import numpy as np
import yaml
from caffe2.python import transformations as tf
from caffe2.python import workspace

import detectron.utils.c2 as c2_utils
import detectron.utils.env as envu
import detectron.utils.net as net_utils
import detectron.utils.subprocess as subprocess_utils
import detectron.utils.vis as vis_utils
from detectron.core.calibrator import (AbsmaxCalib, Calibrator, EMACalib,
                                       KLCalib)
from detectron.core.config import cfg, get_output_dir
from detectron.core.rpn_generator import (generate_rpn_on_dataset,
                                          generate_rpn_on_range)
from detectron.core.test import im_detect_all
from detectron.datasets import task_evaluation
from detectron.datasets.json_dataset import JsonDataset
from detectron.modeling import model_builder
from detectron.utils.io import save_object
from detectron.utils.timer import Timer

logger = logging.getLogger(__name__)


def get_eval_functions():
    """to return the parent and child function handle the inference"""
    # Determine which parent or child function should handle inference
    if cfg.MODEL.RPN_ONLY:
        child_func = generate_rpn_on_range
        parent_func = generate_rpn_on_dataset
    else:
        # Generic case that handles all network types other than RPN-only nets
        # and RetinaNet
        child_func = test_net
        parent_func = test_net_on_dataset

    return parent_func, child_func


def get_inference_dataset(index, is_parent=True):
    """get the dataset of the inference"""
    assert (is_parent or len(cfg.TEST.DATASETS) == 1
            ), "The child inference process can only work on a single dataset"

    dataset_name = cfg.TEST.DATASETS[index]

    if cfg.TEST.PRECOMPUTED_PROPOSALS:
        assert (
            is_parent or len(cfg.TEST.PROPOSAL_FILES) == 1
        ), "The child inference process can only work on a single proposal file"
        assert len(cfg.TEST.PROPOSAL_FILES) == len(cfg.TEST.DATASETS), (
            "If proposals are used, one proposal file must be specified for "
            "each dataset")
        proposal_file = cfg.TEST.PROPOSAL_FILES[index]
    else:
        proposal_file = None

    return dataset_name, proposal_file


def run_inference(
    weights_file,
    ind_range=None,
    multi_gpu_testing=False,
    gpu_id=0,
    check_expected_results=False,
):
    """to run inference"""
    parent_func, child_func = get_eval_functions()
    is_parent = ind_range is None

    def result_getter():
        if is_parent:
            # Parent case:
            # In this case we're either running inference on the entire dataset in a
            # single process or (if multi_gpu_testing is True) using this process to
            # launch subprocesses that each run inference on a range of the dataset
            all_results = {}
            for i in range(len(cfg.TEST.DATASETS)):
                dataset_name, proposal_file = get_inference_dataset(i)
                output_dir = get_output_dir(dataset_name, training=False)
                results = parent_func(
                    weights_file,
                    dataset_name,
                    proposal_file,
                    output_dir,
                    multi_gpu=multi_gpu_testing,
                    gpu_id=gpu_id,
                )
                all_results.update(results)

            return all_results
        else:
            # Subprocess child case:
            # In this case test_net was called via subprocess.Popen to execute on a
            # range of inputs on a single dataset
            dataset_name, proposal_file = get_inference_dataset(
                0, is_parent=False)
            output_dir = get_output_dir(dataset_name, training=False)
            return child_func(
                weights_file,
                dataset_name,
                proposal_file,
                output_dir,
                ind_range=ind_range,
                gpu_id=gpu_id,
            )

    all_results = result_getter()
    if check_expected_results and is_parent:
        task_evaluation.check_expected_results(all_results,
                                               atol=cfg.EXPECTED_RESULTS_ATOL,
                                               rtol=cfg.EXPECTED_RESULTS_RTOL)
        task_evaluation.log_copy_paste_friendly_results(all_results)

    return all_results


def test_net_on_dataset(weights_file,
                        dataset_name,
                        proposal_file,
                        output_dir,
                        multi_gpu=False,
                        gpu_id=0):
    """Run inference on a dataset."""
    dataset = JsonDataset(dataset_name)
    test_timer = Timer()
    test_timer.tic()
    if multi_gpu:
        num_images = len(dataset.get_roidb())
        all_boxes, all_segms, all_keyps = multi_gpu_test_net_on_dataset(
            weights_file, dataset_name, proposal_file, num_images, output_dir)
    else:
        all_boxes, all_segms, all_keyps = test_net(weights_file,
                                                   dataset_name,
                                                   proposal_file,
                                                   output_dir,
                                                   gpu_id=gpu_id)
    test_timer.toc()
    logger.info("Total inference time: {:.3f}s".format(
        test_timer.average_time))
    results = task_evaluation.evaluate_all(dataset, all_boxes, all_segms,
                                           all_keyps, output_dir)
    return results


def multi_gpu_test_net_on_dataset(weights_file, dataset_name, proposal_file,
                                  num_images, output_dir):
    """Multi-gpu inference on a dataset."""
    binary_dir = envu.get_runtime_dir()
    binary_ext = envu.get_py_bin_ext()
    binary = os.path.join(binary_dir, "test_net" + binary_ext)
    assert os.path.exists(binary), "Binary '{}' not found".format(binary)

    # Pass the target dataset and proposal file (if any) via the command line
    opts = ["TEST.DATASETS", '("{}",)'.format(dataset_name)]
    opts += ["TEST.WEIGHTS", weights_file]
    if proposal_file:
        opts += ["TEST.PROPOSAL_FILES", '("{}",)'.format(proposal_file)]

    # Run inference in parallel in subprocesses
    # Outputs will be a list of outputs from each subprocess, where the output
    # of each subprocess is the dictionary saved by test_net().
    outputs = subprocess_utils.process_in_parallel("detection", num_images,
                                                   binary, output_dir, opts)

    # Collate the results from each subprocess
    all_boxes = [[] for _ in range(cfg.MODEL.NUM_CLASSES)]
    all_segms = [[] for _ in range(cfg.MODEL.NUM_CLASSES)]
    all_keyps = [[] for _ in range(cfg.MODEL.NUM_CLASSES)]
    for det_data in outputs:
        all_boxes_batch = det_data["all_boxes"]
        all_segms_batch = det_data["all_segms"]
        all_keyps_batch = det_data["all_keyps"]
        for cls_idx in range(1, cfg.MODEL.NUM_CLASSES):
            all_boxes[cls_idx] += all_boxes_batch[cls_idx]
            all_segms[cls_idx] += all_segms_batch[cls_idx]
            all_keyps[cls_idx] += all_keyps_batch[cls_idx]
    det_file = os.path.join(output_dir, "detections.pkl")
    cfg_yaml = yaml.dump(cfg)
    save_object(
        dict(all_boxes=all_boxes,
             all_segms=all_segms,
             all_keyps=all_keyps,
             cfg=cfg_yaml),
        det_file,
    )
    logger.info("Wrote detections to: {}".format(os.path.abspath(det_file)))

    return all_boxes, all_segms, all_keyps


def test_net(weights_file,
             dataset_name,
             proposal_file,
             output_dir,
             ind_range=None,
             gpu_id=0):
    """Run inference on all images in a dataset or over an index range of images
    in a dataset using a single GPU.
    """
    assert (not cfg.MODEL.RPN_ONLY
            ), "Use rpn_generate to generate proposals from RPN-only models"
    fp32_ws_name = "__fp32_ws__"
    int8_ws_name = "__int8_ws__"
    roidb, dataset, start_ind, end_ind, total_num_images = get_roidb_and_dataset(
        dataset_name, proposal_file, ind_range)
    model1 = None
    if os.environ.get("COSIM"):
        workspace.SwitchWorkspace(int8_ws_name, True)
    model, ob, ob_mask, ob_keypoint = initialize_model_from_cfg(weights_file,
                                                                gpu_id=gpu_id)
    if os.environ.get("COSIM"):
        workspace.SwitchWorkspace(fp32_ws_name, True)
        model1, _, _, _ = initialize_model_from_cfg(weights_file,
                                                    gpu_id=gpu_id,
                                                    int8=False)
    num_images = len(roidb)
    num_classes = cfg.MODEL.NUM_CLASSES
    all_boxes, all_segms, all_keyps = empty_results(num_classes, num_images)
    timers = defaultdict(Timer)

    # for kl_divergence calibration, we use the first 100 images to get
    # the min and max values, and the remaing images are applied to compute the hist.
    # if the len(images) <= 100, we extend the images with themselves.
    if (os.environ.get("INT8INFO") == "1"
            and os.environ.get("INT8CALIB") == "kl_divergence"):
        kl_iter_num_for_range = int(os.environ.get("INT8KLNUM"))
        if not kl_iter_num_for_range:
            kl_iter_num_for_range = 100
        while len(roidb) < 2 * kl_iter_num_for_range:
            roidb += roidb
    if os.environ.get("EPOCH2") == "1":
        for i, entry in enumerate(roidb):
            if cfg.TEST.PRECOMPUTED_PROPOSALS:
                # The roidb may contain ground-truth rois (for example, if the roidb
                # comes from the training or val split). We only want to evaluate
                # detection on the *non*-ground-truth rois. We select only the rois
                # that have the gt_classes field set to 0, which means there's no
                # ground truth.
                box_proposals = entry["boxes"][entry["gt_classes"] == 0]
                if len(box_proposals) == 0:
                    continue
            else:
                # Faster R-CNN type models generate proposals on-the-fly with an
                # in-network RPN; 1-stage models don't require proposals.
                box_proposals = None

            im = []
            im.append(cv2.imread(entry["image"]))
            print("im is {} and i is {} ".format(entry["image"], i))
            with c2_utils.NamedCudaScope(gpu_id):
                cls_boxes_i, cls_segms_i, cls_keyps_i = im_detect_all(
                    model, im, box_proposals, timers, model1)
            extend_results(i, all_boxes, cls_boxes_i[0])
            if cls_segms_i is not None:
                extend_results(i, all_segms, cls_segms_i[0])
            if cls_keyps_i is not None:
                extend_results(i, all_keyps, cls_keyps_i[0])
            all_boxes, all_segms, all_keyps = empty_results(
                num_classes, num_images)
    logging.warning("begin to run benchmark")
    for i, entry in enumerate(roidb):
        if cfg.TEST.PRECOMPUTED_PROPOSALS:
            # The roidb may contain ground-truth rois (for example, if the roidb
            # comes from the training or val split). We only want to evaluate
            # detection on the *non*-ground-truth rois. We select only the rois
            # that have the gt_classes field set to 0, which means there's no
            # ground truth.
            box_proposals = entry["boxes"][entry["gt_classes"] == 0]
            if len(box_proposals) == 0:
                continue
        else:
            # Faster R-CNN type models generate proposals on-the-fly with an
            # in-network RPN; 1-stage models don't require proposals.
            box_proposals = None

        im = []
        im.append(cv2.imread(entry["image"]))
        print("im is {} and i is {} ".format(entry["image"], i))
        with c2_utils.NamedCudaScope(gpu_id):
            cls_boxes_i, cls_segms_i, cls_keyps_i = im_detect_all(
                model, im, box_proposals, timers, model1)
        if os.environ.get("DPROFILE") == "1" and ob != None:
            logging.warning("enter profile log")
            logging.warning("net observer time = {}".format(ob.average_time()))
            logging.warning("net observer time = {}".format(
                ob.average_time_children()))
        if os.environ.get("DPROFILE") == "1" and ob_mask != None:
            logging.warning("mask net observer time = {}".format(
                ob_mask.average_time()))
            logging.warning("mask net observer time = {}".format(
                ob_mask.average_time_children()))
        if os.environ.get("DPROFILE") == "1" and ob_mask != None:
            logging.warning("keypoint net observer time = {}".format(
                ob_keypoint.average_time()))
            logging.warning("keypoint net observer time = {}".format(
                ob_keypoint.average_time_children()))
        extend_results(i, all_boxes, cls_boxes_i[0])
        if cls_segms_i is not None:
            extend_results(i, all_segms, cls_segms_i[0])
        if cls_keyps_i is not None:
            extend_results(i, all_keyps, cls_keyps_i[0])

        if i % 10 == 0:  # Reduce log file size
            ave_total_time = np.sum([t.average_time for t in timers.values()])
            eta_seconds = ave_total_time * (num_images - i - 1)
            eta = str(datetime.timedelta(seconds=int(eta_seconds)))
            det_time = (timers["im_detect_bbox"].average_time +
                        timers["im_detect_mask"].average_time +
                        timers["im_detect_keypoints"].average_time)
            misc_time = (timers["misc_bbox"].average_time +
                         timers["misc_mask"].average_time +
                         timers["misc_keypoints"].average_time)
            logger.info(("im_detect: range [{:d}, {:d}] of {:d}: "
                         "{:d}/{:d} {:.3f}s + {:.3f}s (eta: {})").format(
                             start_ind + 1,
                             end_ind,
                             total_num_images,
                             start_ind + i + 1,
                             start_ind + num_images,
                             det_time,
                             misc_time,
                             eta,
            ))
        if cfg.VIS:
            im_name = os.path.splitext(os.path.basename(entry["image"]))[0]
            vis_utils.vis_one_image(
                im[:, :, ::-1],
                "{:d}_{:s}".format(i, im_name),
                os.path.join(output_dir, "vis"),
                cls_boxes_i[0],
                segms=cls_segms_i[0],
                keypoints=cls_keyps_i[0],
                thresh=cfg.VIS_TH,
                box_alpha=0.8,
                dataset=dataset,
                show_class=True,
            )
        for key, value in timers.items():
            logger.info("{} : {}".format(key, value.average_time))

    # remove observer
    if ob != None:
        model.net.RemoveObserver(ob)
    if ob_mask != None:
        model.mask_net.RemoveObserver(ob_mask)
    if ob_keypoint != None:
        model.keypoint_net.RemoveObserver(ob_keypoint)
    if os.environ.get("INT8INFO") == "1":

        def save_net(net_def, init_def):
            if net_def is None or init_def is None:
                return
            if net_def.name is None or init_def.name is None:
                return
            if os.environ.get("INT8PTXT") == "1":
                with open(net_def.name + "_predict_int8.pbtxt", "wb") as n:
                    n.write(str(net_def))
                with open(net_def.name + "_init_int8.pbtxt", "wb") as n:
                    n.write(str(init_def))
            else:
                with open(net_def.name + "_predict_int8.pb", "wb") as n:
                    n.write(net_def.SerializeToString())
                with open(net_def.name + "_init_int8.pb", "wb") as n:
                    n.write(init_def.SerializeToString())

        algorithm = AbsmaxCalib()
        kind = os.environ.get("INT8CALIB")
        if kind == "moving_average":
            ema_alpha = 0.5
            algorithm = EMACalib(ema_alpha)
        elif kind == "kl_divergence":
            algorithm = KLCalib(kl_iter_num_for_range)
        calib = Calibrator(algorithm)
        if model.net:
            predict_quantized, init_quantized = calib.DepositQuantizedModule(
                workspace, model.net.Proto())
            save_net(predict_quantized, init_quantized)
        if cfg.MODEL.MASK_ON:
            predict_quantized, init_quantized = calib.DepositQuantizedModule(
                workspace, model.mask_net.Proto())
            save_net(predict_quantized, init_quantized)
        if cfg.MODEL.KEYPOINTS_ON:
            predict_quantized, init_quantized = calib.DepositQuantizedModule(
                workspace, model.keypoint_net.Proto())
            save_net(predict_quantized, init_quantized)
    cfg_yaml = yaml.dump(cfg)
    if ind_range is not None:
        det_name = "detection_range_%s_%s.pkl" % tuple(ind_range)
    else:
        det_name = "detections.pkl"
    det_file = os.path.join(output_dir, det_name)
    save_object(
        dict(all_boxes=all_boxes,
             all_segms=all_segms,
             all_keyps=all_keyps,
             cfg=cfg_yaml),
        det_file,
    )
    logger.info("Wrote detections to: {}".format(os.path.abspath(det_file)))
    return all_boxes, all_segms, all_keyps


def initialize_model_from_cfg(weights_file, gpu_id=0, int8=True):
    """Initialize a model from the global cfg. Loads test-time weights and
    creates the networks in the Caffe2 workspace.
    """
    ob = None
    ob_mask = None
    ob_keypoint = None
    model = model_builder.create(cfg.MODEL.TYPE, train=False, gpu_id=gpu_id)
    net_utils.initialize_gpu_from_weights_file(
        model,
        weights_file,
        gpu_id=gpu_id,
    )
    model_builder.add_inference_inputs(model)
    int8_path = os.environ.get("INT8PATH")

    def LoadModuleFile(fname):
        with open(fname) as f:
            from caffe2.proto import caffe2_pb2

            net_def = caffe2_pb2.NetDef()
            if os.environ.get("INT8PTXT") == "1":
                import google.protobuf.text_format as ptxt

                net_def = ptxt.Parse(f.read(), caffe2_pb2.NetDef())
            else:
                net_def.ParseFromString(f.read())
            if gpu_id == -2:
                device_opts = caffe2_pb2.DeviceOption()
                device_opts.device_type = caffe2_pb2.IDEEP
                for op in net_def.op:
                    op.device_option.CopyFrom(device_opts)
            return net_def
        return None

    def CreateNet(net):
        int8_file_path = int8_path if int8_path else ""
        if os.environ.get("INT8PTXT") == "1":
            int8_predict_file = (int8_file_path + "/" + net.Proto().name +
                                 "_predict_int8.pbtxt")
            int8_init_file = (int8_file_path + "/" + net.Proto().name +
                              "_init_int8.pbtxt")
        else:
            int8_predict_file = (int8_file_path + "/" + net.Proto().name +
                                 "_predict_int8.pb")
            int8_init_file = int8_file_path + "/" + net.Proto(
            ).name + "_init_int8.pb"
        if os.path.isfile(int8_init_file):
            logging.warning("Loading Int8 init file for module {}".format(
                net.Proto().name))
            workspace.RunNetOnce(LoadModuleFile(int8_init_file))
        if os.path.isfile(int8_predict_file):
            logging.warning("Loading Int8 predict file for module {}".format(
                net.Proto().name))
            net.Proto().CopyFrom(LoadModuleFile(int8_predict_file))
        if os.environ.get("DEBUGMODE") == "1":
            for i, op in enumerate(net.Proto().op):
                if len(op.name) == 0:
                    op.name = op.type.lower() + str(i)
        if gpu_id == -2 and os.environ.get("DNOOPT") != "1":
            logging.warning("Optimize module {}....................".format(
                net.Proto().name))
            tf.optimizeForIDEEP(net)
        if os.environ.get("DEBUGMODE") == "1":
            with open("{}_opt_predict_net.pb".format(net.Proto().name),
                      "w") as fid:
                fid.write(net.Proto().SerializeToString())
            with open("{}_opt_predict_net.pbtxt".format(net.Proto().name),
                      "w") as fid:
                fid.write(str(net.Proto()))
        workspace.CreateNet(net)

    if os.environ.get("COSIM") and int8 == False:
        int8_path = None
    CreateNet(model.net)
    if os.environ.get("DPROFILE") == "1":
        logging.warning("need profile, add observer....................")
        ob = model.net.AddObserver("TimeObserver")
    workspace.CreateNet(model.conv_body_net)
    if cfg.MODEL.MASK_ON:
        CreateNet(model.mask_net)
        if os.environ.get("DPROFILE") == "1":
            ob_mask = model.mask_net.AddObserver("TimeObserver")
    if cfg.MODEL.KEYPOINTS_ON:
        CreateNet(model.keypoint_net)
        if os.environ.get("DPROFILE") == "1":
            ob_keypoint = model.keypoint_net.AddObserver("TimeObserver")
    return model, ob, ob_mask, ob_keypoint


def get_roidb_and_dataset(dataset_name, proposal_file, ind_range):
    """Get the roidb for the dataset specified in the global cfg. Optionally
    restrict it to a range of indices if ind_range is a pair of integers.
    """
    dataset = JsonDataset(dataset_name)
    if cfg.TEST.PRECOMPUTED_PROPOSALS:
        assert proposal_file, "No proposal file given"
        roidb = dataset.get_roidb(proposal_file=proposal_file,
                                  proposal_limit=cfg.TEST.PROPOSAL_LIMIT)
    else:
        roidb = dataset.get_roidb()

    if ind_range is not None:
        total_num_images = len(roidb)
        start, end = ind_range
        roidb = roidb[start:end]
    else:
        start = 0
        end = len(roidb)
        total_num_images = end

    return roidb, dataset, start, end, total_num_images


def empty_results(num_classes, num_images):
    """Return empty results lists for boxes, masks, and keypoints.
    Box detections are collected into:
      all_boxes[cls][image] = N x 5 array with columns (x1, y1, x2, y2, score)
    Instance mask predictions are collected into:
      all_segms[cls][image] = [...] list of COCO RLE encoded masks that are in
      1:1 correspondence with the boxes in all_boxes[cls][image]
    Keypoint predictions are collected into:
      all_keyps[cls][image] = [...] list of keypoints results, each encoded as
      a 3D array (#rois, 4, #keypoints) with the 4 rows corresponding to
      [x, y, logit, prob] (See: utils.keypoints.heatmaps_to_keypoints).
      Keypoints are recorded for person (cls = 1); they are in 1:1
      correspondence with the boxes in all_boxes[cls][image].
    """
    # Note: do not be tempted to use [[] * N], which gives N references to the
    # *same* empty list.
    all_boxes = [[[] for _ in range(num_images)] for _ in range(num_classes)]
    all_segms = [[[] for _ in range(num_images)] for _ in range(num_classes)]
    all_keyps = [[[] for _ in range(num_images)] for _ in range(num_classes)]
    return all_boxes, all_segms, all_keyps


def extend_results(index, all_res, im_res):
    """Add results for an image to the set of all results at the specified
    index.
    """
    # Skip cls_idx 0 (__background__)
    for cls_idx in range(1, len(im_res)):
        all_res[cls_idx][index] = im_res[cls_idx]
