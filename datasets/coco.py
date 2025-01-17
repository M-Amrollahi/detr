# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
"""
COCO dataset which returns image_id for evaluation.

Mostly copy-paste from https://github.com/pytorch/vision/blob/13b35ff/references/detection/coco_utils.py
"""
from pathlib import Path

import torch
import torch.utils.data
import torchvision
import numpy as np
from pycocotools import mask as coco_mask

import albumentations as A
import torchvision.transforms as T
from albumentations.pytorch.transforms import ToTensorV2
from albumentations.augmentations.transforms import Normalize

from util.box_ops import box_xyxy_to_cxcywh

#import datasets.transforms as T

class CocoDetection(torchvision.datasets.CocoDetection):
    def __init__(self, img_folder, ann_file, transforms, return_masks):
        super(CocoDetection, self).__init__(img_folder, ann_file)
        self._transforms = transforms
        self.prepare = ConvertCocoPolysToMask(return_masks)

    def __getitem__(self, idx):
        img, target = super(CocoDetection, self).__getitem__(idx)
        image_id = self.ids[idx]
        
        target = {'image_id': image_id, 'annotations': target}
        img, target = self.prepare(img, target)
        
        if self._transforms is not None:
            tmp_boxes = target["boxes"].numpy()
            tmp_labels = target["labels"].numpy()
            
            _res = self._transforms(
                image = np.array(img),
                bboxes = np.hstack((tmp_boxes, tmp_labels[ : , np.newaxis])))
            
            img = _res["image"].float()
            target["boxes"] = torch.tensor(_res["bboxes"], dtype=torch.float32)[:,:4]
            target["labels"] = torch.tensor(_res["bboxes"], dtype=torch.int64)[:,4]
            
            target["boxes"] = box_xyxy_to_cxcywh(target["boxes"]) / 640.0
            
        return img, target


def convert_coco_poly_to_mask(segmentations, height, width):
    masks = []
    for polygons in segmentations:
        rles = coco_mask.frPyObjects(polygons, height, width)
        mask = coco_mask.decode(rles)
        if len(mask.shape) < 3:
            mask = mask[..., None]
        mask = torch.as_tensor(mask, dtype=torch.uint8)
        mask = mask.any(dim=2)
        masks.append(mask)
    if masks:
        masks = torch.stack(masks, dim=0)
    else:
        masks = torch.zeros((0, height, width), dtype=torch.uint8)
    return masks


class ConvertCocoPolysToMask(object):
    def __init__(self, return_masks=False):
        self.return_masks = return_masks

    def __call__(self, image, target):
        w, h = image.size

        image_id = target["image_id"]
        image_id = torch.tensor([image_id])

        anno = target["annotations"]

        anno = [obj for obj in anno if 'iscrowd' not in obj or obj['iscrowd'] == 0]

        boxes = [obj["bbox"] for obj in anno]
        # guard against no boxes via resizing
        boxes = torch.as_tensor(boxes, dtype=torch.float32).reshape(-1, 4)
        boxes[:, 2:] += boxes[:, :2]
        boxes[:, 0::2].clamp_(min=0, max=w)
        boxes[:, 1::2].clamp_(min=0, max=h)

        classes = [obj["category_id"] for obj in anno]
        classes = torch.tensor(classes, dtype=torch.int64)

        if self.return_masks:
            segmentations = [obj["segmentation"] for obj in anno]
            masks = convert_coco_poly_to_mask(segmentations, h, w)

        keypoints = None
        if anno and "keypoints" in anno[0]:
            keypoints = [obj["keypoints"] for obj in anno]
            keypoints = torch.as_tensor(keypoints, dtype=torch.float32)
            num_keypoints = keypoints.shape[0]
            if num_keypoints:
                keypoints = keypoints.view(num_keypoints, -1, 3)

        keep = (boxes[:, 3] > boxes[:, 1]) & (boxes[:, 2] > boxes[:, 0])
        boxes = boxes[keep]
        classes = classes[keep]
        if self.return_masks:
            masks = masks[keep]
        if keypoints is not None:
            keypoints = keypoints[keep]

        target = {}
        target["boxes"] = boxes
        target["labels"] = classes
        if self.return_masks:
            target["masks"] = masks
        target["image_id"] = image_id
        if keypoints is not None:
            target["keypoints"] = keypoints

        # for conversion to coco api
        area = torch.tensor([obj["area"] for obj in anno])
        iscrowd = torch.tensor([obj["iscrowd"] if "iscrowd" in obj else 0 for obj in anno])
        target["area"] = area[keep]
        target["iscrowd"] = iscrowd[keep]

        target["orig_size"] = torch.as_tensor([int(h), int(w)])
        target["size"] = torch.as_tensor([int(h), int(w)])

        return image, target


def make_coco_transforms(image_set):

    trans_val = A.Compose(
        [
            A.Resize(height=640, width=640),
            Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ToTensorV2(),
        ])
    
    trans_train = A.Compose(
        [
            A.VerticalFlip(p = .5),
            A.HorizontalFlip(p = .5),
            A.Sharpen(alpha=(.3, .3),p = .5),
            A.RandomBrightnessContrast(brightness_limit = 0.15, contrast_limit = 0.15, p = .4),
            A.ColorJitter(p = 0.7 , saturation=(0.01,3.0) , hue=(-0.3,0.3) , contrast=(1.0,5.0), brightness=(1.,1.5)),
            A.RandomGamma(gamma_limit = (80, 120),p = .5),
            A.GaussNoise(var_limit = (1,30),  mean = 0, p = .5),
            #A.Downscale(scale_min = 0.80, scale_max = 0.99, p = 0.5),
            #A.PixelDropout(dropout_prob = .05, p = 0.5, drop_value = 127),
            #A.Rotate(limit = (-30,30), interpolation = 4, border_mode = 2, p = 0.5),
            A.CLAHE(p = .5),
            #A.Resize(height = 320, width = 320),
            #A.RandomCrop(height = 320,width=320, p=1.0)

            trans_val
        ],
            bbox_params=A.BboxParams(format='pascal_voc')
        )

    if image_set == 'train':
        return trans_train

    if image_set == 'val':
        return trans_val
    
    raise ValueError(f'unknown {image_set}')


def build(image_set, args):
    root = Path(args.coco_path)
    assert root.exists(), f'provided COCO path {root} does not exist'
    mode = 'instances'
    PATHS = {
        "train": (root / "train2017", root / "annotations" / f'{mode}_train2017.json'),
        "val": (root / "val2017", root / "annotations" / f'{mode}_val2017.json'),
    }

    img_folder, ann_file = PATHS[image_set]
    dataset = CocoDetection(img_folder, ann_file, transforms=make_coco_transforms(image_set), return_masks=args.masks)
    return dataset
