# ------------------------------------------------------------------------------
# Copyright (c) Microsoft
# Licensed under the MIT License.
# Written by Bin Xiao (Bin.Xiao@microsoft.com)
# ------------------------------------------------------------------------------

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import logging
import random

import cv2
import numpy as np
import torch
from torch.utils.data import Dataset
from pycocotools.coco import COCO

from utils.transforms import get_affine_transform
from utils.transforms import affine_transform
from utils.transforms import fliplr_joints

logger = logging.getLogger(__name__)


class JointsDataset(Dataset):
    def __init__(self, cfg, root, image_set, is_train, transform=None):
        self.num_joints = 0
        self.pixel_std = 200
        self.flip_pairs = []
        self.parent_ids = []

        self.is_train = is_train
        self.root = root
        self.image_set = image_set

        self.output_path = cfg.OUTPUT_DIR
        self.data_format = cfg.DATASET.DATA_FORMAT

        self.scale_factor = cfg.DATASET.SCALE_FACTOR
        self.rotation_factor = cfg.DATASET.ROT_FACTOR
        self.flip = cfg.DATASET.FLIP
        self.num_joints_half_body = cfg.DATASET.NUM_JOINTS_HALF_BODY
        self.prob_half_body = cfg.DATASET.PROB_HALF_BODY
        self.color_rgb = cfg.DATASET.COLOR_RGB

        self.target_type = cfg.MODEL.TARGET_TYPE
        self.image_size = np.array(cfg.MODEL.IMAGE_SIZE)
        self.heatmap_size = np.array(cfg.MODEL.HEATMAP_SIZE)

        self.coco = COCO()
        self.sigma = cfg.MODEL.SIGMA
        # coco default sigmas
        self.sigmas = np.array(
            [
                .26, .25, .25, .35, .35, .79, .79, .72, .72, .62,
                .62, 1.07, 1.07, .87, .87, .89, .89
            ]
        ) / 10.0
        self.use_different_joints_weight = cfg.LOSS.USE_DIFFERENT_JOINTS_WEIGHT
        self.joints_weight = 1.0
        self.aspect_ratio = self.image_size[0] * 1.0 / self.image_size[1]
        self.transform = transform

        self.id2name, self.name2id = self._get_mapping_id_name(self.coco.imgs)
        self.db = []

    @staticmethod
    def _get_mapping_id_name(imgs):
        '''
        Args: imgs (dict): dict of image info

        Returns:
            tuple: Image name & id mapping dicts.

            - id2name (dict): mapping image id to name.
            - name2id (dict): mapping image name to id.
        '''
        id2name = {}
        name2id = {}
        for image_id, image in imgs.items():
            file_name = image['file_name']
            id2name[image_id] = file_name
            name2id[file_name] = image_id

        return id2name, name2id

    def _get_db(self):
        raise NotImplementedError

    def evaluate(self, cfg, preds, output_dir, *args, **kwargs):
        raise NotImplementedError

    def half_body_transform(self, joints, joints_vis):
        '''
        随机保留上半生或下半身，一种data augmentation
        Args:
            - joints: 关键点位置, shape=[17, 3]，因为使用2d表示，第3维都为0
            - joints_vis：关键点是否可见，shape=[17, 3]

        Returns:
            - center, scale
        '''
        upper_joints = []
        lower_joints = []
        for joint_id in range(self.num_joints):
            if joints_vis[joint_id][0] > 0:
                if joint_id in self.upper_body_ids:
                    upper_joints.append(joints[joint_id])
                else:
                    lower_joints.append(joints[joint_id])

        if np.random.randn() < 0.5 and len(upper_joints) > 2:
            selected_joints = upper_joints
        else:
            selected_joints = lower_joints \
                if len(lower_joints) > 2 else upper_joints

        if len(selected_joints) < 2:
            return None, None

        selected_joints = np.array(selected_joints, dtype=np.float32)
        # 关键点x,y的平均值
        center = selected_joints.mean(axis=0)[:2]

        left_top = np.amin(selected_joints, axis=0)
        right_bottom = np.amax(selected_joints, axis=0)

        # 包含所有关键点的最小宽和高
        w = right_bottom[0] - left_top[0] + 1
        h = right_bottom[1] - left_top[1] + 1

        if w > self.aspect_ratio * h:
            h = w * 1.0 / self.aspect_ratio
        elif w < self.aspect_ratio * h:
            w = h * self.aspect_ratio

        scale = np.array(
            [
                w * 1.0 / self.pixel_std,
                h * 1.0 / self.pixel_std
            ],
            dtype=np.float32
        )

        scale = scale * 1.5

        return center, scale

    def __len__(self, ):
        return len(self.db)

    def __getitem__(self, idx):
        db_rec = copy.deepcopy(self.db[idx])

        image_file = db_rec['image']
        # filename, imgnum暂时没用
        filename = db_rec['filename'] if 'filename' in db_rec else ''
        imgnum = db_rec['imgnum'] if 'imgnum' in db_rec else ''

        if self.data_format == 'zip':
            from utils import zipreader
            data_numpy = zipreader.imread(
                image_file, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION
            )
        # 直接读像素值
        else:
            data_numpy = cv2.imread(
                image_file, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION
            )

        if data_numpy is None:
            logger.error('=> fail to read {}'.format(image_file))
            raise ValueError('Fail to read {}'.format(image_file))

        if self.color_rgb:
            data_numpy = cv2.cvtColor(data_numpy, cv2.COLOR_BGR2RGB)

        joints = db_rec['joints_3d']
        joints_vis = db_rec['joints_3d_vis']

        c = db_rec['center']
        s = db_rec['scale']
        # 因为是直接提供的bbox，所以bbox_score设为1
        bbox_score = db_rec['bbox_score'] if 'bbox_score' in db_rec else 1
        bbox_id = db_rec['bbox_id']
        r = 0

        if self.is_train:
            if (np.sum(joints_vis[:, 0]) > self.num_joints_half_body
                    and np.random.rand() < self.prob_half_body):
                c_half_body, s_half_body = self.half_body_transform(
                    joints, joints_vis
                )

                if c_half_body is not None and s_half_body is not None:
                    c, s = c_half_body, s_half_body

            # scale factor, rotation factor
            sf = self.scale_factor
            rf = self.rotation_factor

            s = s * np.clip(np.random.randn() * sf + 1, 1 - sf, 1 + sf)  # random scale([0.65, 1.35])
            r = np.clip(np.random.randn() * rf, -rf * 2, rf * 2) \
                if random.random() <= 0.6 else 0  # random rotation([-45°, 45°])

            if self.flip and random.random() <= 0.5:
                data_numpy = data_numpy[:, ::-1, :]
                joints, joints_vis = fliplr_joints(
                    joints, joints_vis, data_numpy.shape[1], self.flip_pairs)
                c[0] = data_numpy.shape[1] - c[0] - 1

        joints_heatmap = joints.copy()
        trans = get_affine_transform(c, s, r, self.image_size)
        trans_heatmap = get_affine_transform(c, s, r, self.heatmap_size)
        input = cv2.warpAffine(
            data_numpy,
            trans,
            (int(self.image_size[0]), int(self.image_size[1])),
            flags=cv2.INTER_LINEAR)

        if self.transform:
            input = self.transform(input)

        for i in range(self.num_joints):
            if joints_vis[i, 0] > 0.0:
                joints[i, 0:2] = affine_transform(joints[i, 0:2], trans)
                joints_heatmap[i, 0:2] = affine_transform(joints_heatmap[i, 0:2], trans_heatmap)

        # 获得gt，target ndarray(num_joints, 64, 64), target_weight ndarray(num_joints, 1)
        # 与hrnet源码不同，这边encoding过程是在heatmap的尺寸上直接生成heatmap，避免了量化损失，不用除feat_stride
        # 这边就是darkpose的encoding全过程
        target, target_weight = self.generate_target(joints_heatmap, joints_vis)
        # max_value=torch.max(target)
        # target = torch.from_numpy(target)
        # target_weight = target_weight.detach().numpy
        # target_weight = torch.from_numpy(target_weight)

        meta = {
            'image': image_file,
            'filename': filename,
            'imgnum': imgnum,
            'joints': joints,
            'joints_vis': joints_vis,
            'center': c,
            'scale': s,
            'rotation': r,
            'bbox_score': bbox_score,
            'bbox_id': bbox_id
        }

        return input, target, target_weight, meta

    def select_data(self, db):
        db_selected = []
        for rec in db:
            num_vis = 0
            joints_x = 0.0
            joints_y = 0.0
            for joint, joint_vis in zip(
                    rec['joints_3d'], rec['joints_3d_vis']):
                if joint_vis[0] <= 0:
                    continue
                num_vis += 1

                joints_x += joint[0]
                joints_y += joint[1]
            if num_vis == 0:
                continue

            joints_x, joints_y = joints_x / num_vis, joints_y / num_vis

            area = rec['scale'][0] * rec['scale'][1] * (self.pixel_std ** 2)
            joints_center = np.array([joints_x, joints_y])
            bbox_center = np.array(rec['center'])
            diff_norm2 = np.linalg.norm((joints_center - bbox_center), 2)
            ks = np.exp(-1.0 * (diff_norm2 ** 2) / ((0.2) ** 2 * 2.0 * area))

            metric = (0.2 / 16) * num_vis + 0.45 - 0.2 / 16
            if ks > metric:
                db_selected.append(rec)

        logger.info('=> num db: {}'.format(len(db)))
        logger.info('=> num selected db: {}'.format(len(db_selected)))
        return db_selected

    def generate_target(self, joints, joints_vis):
        '''
        :param joints:  [num_joints, 3] joints的2d位置
        :param joints_vis: [num_joints, 3]
        :return: target, target_weight(1: visible, 0: invisible)
        '''
        joints = torch.from_numpy(joints)
        joints_vis = torch.from_numpy(joints_vis)
        target_weight = torch.ones((self.num_joints, 1), dtype=torch.float32)
        target_weight[:, 0] = joints_vis[:, 0]

        assert self.target_type == 'gaussian', \
            'Only support gaussian map now!'

        # gaussian生成heatmap
        if self.target_type == 'gaussian':
            target = torch.zeros((self.num_joints,
                                  self.heatmap_size[1],
                                  self.heatmap_size[0]),
                                 dtype=torch.float32)

            # sigma默认是2，tmp_size这里就是6
            tmp_size = self.sigma * 3

            for joint_id in range(self.num_joints):
                target_weight[joint_id] = self.adjust_target_weight(joints[joint_id], target_weight[joint_id], tmp_size)
                if target_weight[joint_id] == 0:
                    continue

                mu_x = joints[joint_id][0]
                mu_y = joints[joint_id][1]

                x = torch.arange(0, self.heatmap_size[0], 1, dtype=torch.float32)
                y = torch.arange(0, self.heatmap_size[1], 1, dtype=torch.float32)
                y = torch.unsqueeze(y, dim=1)

                v = target_weight[joint_id]
                if v > 0.5:
                    target[joint_id] = torch.exp(-((x - mu_x) ** 2 + (y - mu_y) ** 2) / (2 * self.sigma ** 2))

        if self.use_different_joints_weight:
            target_weight = np.multiply(target_weight, self.joints_weight)

        return target, target_weight

    def adjust_target_weight(self, joint, target_weight, tmp_size):
        # feat_stride = self.image_size / self.heatmap_size
        mu_x = joint[0]
        mu_y = joint[1]
        # Check that any part of the gaussian is in-bounds
        ul = [int(mu_x - tmp_size), int(mu_y - tmp_size)]
        br = [int(mu_x + tmp_size + 1), int(mu_y + tmp_size + 1)]
        if ul[0] >= self.heatmap_size[0] or ul[1] >= self.heatmap_size[1] \
                or br[0] < 0 or br[1] < 0:
            # If not, just return the image as is
            target_weight = 0

        return target_weight
