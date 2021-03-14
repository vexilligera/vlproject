# datautils.py : linear psd loading/rendering utils and basic augmentations
import random
import io
import os
import pickle
from PIL import Image, ImageOps
import numpy as np
import cv2
import psd_tools
from psd_tools import PSDImage
from psd_tools.constants import BlendMode
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms
import torchvision.transforms.functional as TF
from matplotlib import pyplot as plt
from multiprocessing import Pool


def psd2pil(psd):
    # converts psd layers into pil list for augmentation
    layers = []
    width, height = psd.width, psd.height

    def pad_to_canvas(layer):
        x1, y1, x2, y2 = layer.bbox
        return ImageOps.expand(layer.topil(), border=(x1, y1, width - x2, height - y2), fill=0)

    for i, layer in enumerate(psd):
        if layer.size[0] == 0 or layer.size[1] == 0:
            continue
        layer_img = pad_to_canvas(layer)
        if layer_img.getbbox() is None:  # filter empty layers
            continue
        layers.append([layer.name, layer_img, layer.opacity, layer.visible, layer.blend_mode, False])
        if len(layer.clip_layers):
            for clip in layer.clip_layers:
                clip_img = pad_to_canvas(clip)
                if clip_img.getbbox() is None:
                    continue
                layers.append([clip.name, clip_img, clip.opacity, clip.visible, clip.blend_mode, True])

    return layers


def pil2tensor(data, layer_tensor_filter=None, remove_occluded=False):
    # converts pil list to tensors, normalize opacity, removes occluded pixels
    layers = data['layers']
    preview = np.array(data['preview']).astype(np.float32) / 255.0
    for i in range(len(layers)):
        if layers[i][1].getbbox() is not None and layers[i][2] > 0 and layers[i][3]:
            layers[i][1] = np.array(layers[i][1]).astype(np.float32) / 255.0
            if layers[i][1].shape[2] > 3:
                layers[i][1][:, :, 3] *= layers[i][2] / 255.0
        else:
            # remove invisible or 100% transparent layers
            layers[i][1] = None

    layer_tensors = []
    cur_layer_img = None
    for name, img, opacity, visible, blend_mode, is_clip in layers:
        if img is None:
            continue
        if cur_layer_img is None or not is_clip:
            cur_layer_img = img
        if is_clip:
            img[:, :, 3] *= cur_layer_img[:, :, 3]
            img[img[:, :, 3] == 0] = 0
        layer_tensors.append((img, blend_mode))

    if layer_tensor_filter is not None:
        layer_tensors = layer_tensor_filter(layer_tensors, preview)

    if remove_occluded:
        h, w, c = layer_tensors[0][0].shape
        mask = np.zeros((h, w), dtype=np.uint8)
        for img, blend_mode in reversed(layer_tensors):
            if blend_mode != BlendMode.NORMAL:
                continue
            img[mask == 1] = 0
            if img.shape[2] > 3:
                mask[img[:, :, 3] == 1] = 1

    return layer_tensors, preview


# drops out full layers
def drop_full_post_filter(layer_tensors, preview):
    tmp = []
    for img, blend_mode in layer_tensors:
        tmp.append(img)
    t = np.stack(tmp)
    p = np.mean(preview[:, :, 3])
    t = np.mean(t[:, :, :, 3], axis=(1, 2)) / p
    ret = []
    for i, (img, blend_mode) in enumerate(layer_tensors):
        if (t[i] > 0.95 and i > len(layer_tensors) - 2) or t[i] < 1e-5:
            continue
        ret.append((img, blend_mode))
    return ret


def default_post_filter(layer_tensors, preview):
    return layer_tensors


def image_to_byte_array(image):
    imgByteArr = io.BytesIO()
    image.save(imgByteArr, format='PNG')
    imgByteArr = imgByteArr.getvalue()
    return imgByteArr


def load_from_pickle(path):
    f = open(path, 'rb')
    data = pickle.load(f)
    data['preview'] = Image.open(io.BytesIO(data['preview']))
    for i in range(len(data['layers'])):
        data['layers'][i][1] = Image.open(io.BytesIO(data['layers'][i][1]))
    f.close()
    return data


'''
    data augmentation and loader
'''

BLEND_DICT = {
    BlendMode.NORMAL: 0,
    BlendMode.MULTIPLY: 1,
    BlendMode.LINEAR_DODGE: 2,
    BlendMode.SCREEN: 3,
    b'padding': 4
}


class PSDBasicAugmentation:
    # data augmentation on pil layers
    def __init__(self, target_size, angle=0, top=0.0, left=0.0, height=1.0, width=1.0, hflip=False, vflip=False, scale=0.4):
        self.set_params(target_size, angle, top, left, height, width, hflip, vflip, scale)

    def set_params(self, target_size, angle, top, left, height, width, hflip, vflip, scale):
        self.angle = angle
        if isinstance(target_size, (int, float)):
            target_size = (target_size, target_size)
        self.target_size = target_size
        self.top = top
        self.left = left
        self.height = height
        self.width = width
        self.hflip = hflip
        self.vflip = vflip
        self.scale = scale

    def roll(self):
        self.angle = random.randint(-90, 90)
        self.top = np.random.random() * 0.05
        self.left = np.random.random() * 0.05
        self.height = np.clip(1.0 + np.random.randn() / 50, 0.5, 1.5)
        self.width = np.clip(self.height + np.random.randn() / 50, 0.5, 1.5)
        self.hflip = random.random() < 0.5
        self.vflip = random.random() < 0.1
        self.scale = random.random()
        return self

    def apply(self, x):
        w, h = x.size
        scale = (self.scale / 2 + 0.8) * min(self.target_size) / min(w, h)
        x = TF.resize(x, (int(h * self.height * scale), int(w * self.width * scale)))
        x = TF.rotate(x, self.angle)
        x = TF.crop(x, self.top, self.left, self.target_size[1], self.target_size[0])
        if self.hflip:
            x = TF.hflip(x)
        if self.vflip:
            x = TF.vflip(x)
        return x

    def __call__(self, data):
        # layers: list of (name, RGBA PIL images, opacity, visible, blend_mode, is_clip)
        transformed_layers = []
        for name, x, opacity, visible, blend_mode, is_clip in data['layers']:
            x = self.apply(x)
            transformed_layers.append([name, x, opacity, visible, blend_mode, is_clip])
        ret = {
            'preview': self.apply(data['preview']),
            'layers': transformed_layers
        }
        return ret


class PSDPickleDataset(Dataset):
    # ./root_dir
    #     - sample 1
    #       - 0.pkl
    #       - 1.pkl
    def __init__(self, root_dir, max_layers=64, transform=None, post_filter=default_post_filter):
        self.root_dir = root_dir
        self.transform = transform
        self.post_filter = post_filter
        self.max_layers = max_layers
        
        self.paths = []
        for sample in os.listdir(root_dir):
            n = len(os.listdir(os.path.join(root_dir, sample)))
            self.paths += [os.path.join(sample, '%d.pkl' % (i)) for i in range(n)]

    def load_pkl_data(self, path, remove_occluded, warp, resize_to_final):
        sample = load_from_pickle(path)
        M = np.array(sample['transform'])
        final_width, final_height = sample['finishing_size']
        w, h = sample['preview'].size
        if warp and np.linalg.norm(M - np.eye(3)) > 5e-3:
            warped_layers = []
            for name, x, opacity, visible, blend_mode, is_clip in sample['layers']:
                x = np.array(x, dtype=np.float32)
                x = cv2.warpPerspective(x, M, (final_width, final_height))
                x = Image.fromarray(x.astype(np.uint8))
                warped_layers.append([name, x, opacity, visible, blend_mode, is_clip])
            sample['layers'] = warped_layers
        elif resize_to_final and (w != final_width or h != final_height):
            for i in range(len(sample['layers'])):
                sample['layers'][i][1] = sample['layers'][i][1].resize((final_width, final_height), Image.ANTIALIAS)

        if self.transform:
            self.transform.roll()
            sample = self.transform(sample)
        layers, preview = pil2tensor(sample, self.post_filter, remove_occluded)
        tensors, blend_modes = [], []
        for layer, blend_mode in layers:
            tensors.append(layer)
            blend_modes.append(blend_mode)
        while len(tensors) > self.max_layers:
            idx = random.randint(0, len(tensors) - 1)
            del tensors[idx]
            del blend_modes[idx]
        tensors = np.stack(tensors)
        l, h, w, c = tensors.shape
        blend_modes = np.array([BLEND_DICT[i] for i in blend_modes], dtype=np.uint8)
        if len(tensors) < self.max_layers:
            padding = np.zeros((self.max_layers - len(tensors), h, w, c), dtype=np.float32)
            blend_padding = np.ones(self.max_layers - len(tensors), dtype=np.uint8) * BLEND_DICT[b'padding']
            blend_modes = np.concatenate((blend_padding, blend_modes))
            tensors = np.concatenate((padding, tensors))
        tensors = tensors.transpose((0, 3, 1, 2))
        one_hot_bm = np.identity(len(BLEND_DICT))[blend_modes]
        one_hot_bm[one_hot_bm[:, BLEND_DICT[b'padding']] == 1, BLEND_DICT[BlendMode.NORMAL]] = 1
        return tensors, one_hot_bm, len(sample['layers'])

    def load_preview_data(self, path, warp, resize_to_final, transform=None):
        sample = load_from_pickle(path)
        M = np.array(sample['transform'])
        final_width, final_height = sample['finishing_size']
        preview = sample['preview']
        w, h = preview.size

        if warp and np.linalg.norm(M - np.eye(3)) > 5e-3:
            preview = np.array(preview).astype(np.float32)
            preview = cv2.warpPerspective(preview, M, (final_width, final_height))
            preview = Image.fromarray(preview.astype(np.uint8))
        elif resize_to_final and (w != final_width or h != final_height):
            preview = preview.resize((final_width, final_height), Image.ANTIALIAS)

        if transform:
            preview = transform.apply(preview)
        elif self.transform:
            self.transform.roll()
            preview = self.transform.apply(preview)

        preview = np.array(preview).astype(np.float32) / 255.
        return preview[:, :, :3].transpose((2, 0, 1))


def normalize_img(img):
    return 2 * (img - 0.5)


def denormalize_img(img):
    return ((img + 1) / 2) * 255


class PSDPreviewPairDataset(PSDPickleDataset):
    def __init__(self, root_dir, transform=None, warp=True, resize_to_final=True):
        super(PSDPreviewPairDataset, self).__init__(root_dir, 0, transform, default_post_filter)
        self.warp = warp
        self.resize_to_final = resize_to_final

    def __len__(self):
        return len(self.paths)

    def __getitem__(self, idx):
        path = self.paths[idx]
        sample_name = os.path.split(self.paths[idx])[0]
        cnt = idx
        while cnt < len(self.paths) and os.path.split(self.paths[cnt])[0] == sample_name:
            cnt += 1
        cnt -= 1
        last_path = self.paths[cnt]
        path = os.path.join(self.root_dir, path)
        last_path = os.path.join(self.root_dir, last_path)
        if self.transform:
            self.transform.roll()
        preview_cur = self.load_preview_data(path, self.warp, self.resize_to_final, transform=self.transform)
        preview_last = self.load_preview_data(last_path, self.warp, self.resize_to_final, transform=self.transform)
        # normalize to [-1, 1]
        preview_cur = normalize_img(preview_cur)
        preview_last = normalize_img(preview_last)
        return preview_cur, preview_last


def psd2pickle(psd, save_path, attrs={}):
    layers = psd2pil(psd)
    preview =  image_to_byte_array(psd.composite())
    save_layers = []
    for name, img, opacity, visible, blend_mode, is_clip in layers:
        save_layers.append([name, image_to_byte_array(img), opacity, visible, blend_mode, is_clip])
    data = {
        'preview': preview,
        'layers': save_layers
    }
    data.update(attrs)
    f = open(save_path, 'wb')
    pickle.dump(data, f)
    f.close()


def iou(boxA, boxB):
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])

    interArea = abs(max((xB - xA, 0)) * max((yB - yA), 0))
    if interArea == 0:
        return 0

    boxAArea = abs((boxA[2] - boxA[0]) * (boxA[3] - boxA[1]))
    boxBArea = abs((boxB[2] - boxB[0]) * (boxB[3] - boxB[1]))

    iou = interArea / float(boxAArea + boxBArea - interArea)

    return iou


def dataset_psd2pkl_worker(args):
    path, files, delete_psd = args
    if len(files) == 0:
        return
    files = ['%d.psd' % (j) for j in sorted([int(i.split('.')[0]) for i in files])[::-1]]
    name = files[0].split('.')[0]
    psd_path = os.path.join(path, files[0])
    pkl_path = os.path.join(path, name + '.pkl')
    reference = PSDImage.open(psd_path)
    M = np.eye(3, dtype=np.float32)
    final_width, final_height = reference.size

    reference_img = np.array(ImageOps.grayscale(reference.composite()))
    MIN_MATCH_COUNT = 10
    ltol, rtol = 5e-3, 200
    eye = np.eye(3, dtype=np.float32)
    psd2pickle(reference, pkl_path, {'finishing_size': [final_width, final_height], 'transform': M.tolist()})
    if delete_psd:
        os.remove(psd_path)

    if len(files) == 0:
        return

    for f in files[1:]:
        name = f.split('.')[0]
        psd_path = os.path.join(path, f)
        pkl_path = os.path.join(path, name + '.pkl')
        current = PSDImage.open(psd_path)
        w, h = current.size
        current_img = np.array(ImageOps.grayscale(current.composite()))

        # Initiate SIFT detector
        sift = cv2.SIFT_create()

        # find the keypoints and descriptors with SIFT
        kp1, des1 = sift.detectAndCompute(current_img, None)
        kp2, des2 = sift.detectAndCompute(reference_img, None)
        FLANN_INDEX_KDTREE = 0
        index_params = dict(algorithm = FLANN_INDEX_KDTREE, trees = 5)
        search_params = dict(checks = 50)

        flann = cv2.FlannBasedMatcher(index_params, search_params)

        matches = flann.knnMatch(des1,des2,k=2)

        # store all the good matches as per Lowe's ratio test.
        good = []
        for m,n in matches:
            if m.distance < 0.7*n.distance:
                good.append(m)

        if len(good) > MIN_MATCH_COUNT:
            src_pts = np.float32([ kp1[m.queryIdx].pt for m in good ]).reshape(-1,1,2)
            dst_pts = np.float32([ kp2[m.trainIdx].pt for m in good ]).reshape(-1,1,2)

            perspective, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
        else:
            mask = None
            perspective = np.eye(3)
        
        M = M @ perspective
        if np.linalg.norm(M - eye) < ltol:
            M = np.eye(3)
        else:
            src_rect = np.float32([ [0,0],[0,h-1],[w-1,h-1],[w-1,0] ]).reshape(-1,1,2)
            dst = cv2.perspectiveTransform(src_rect, M)
            dst_rect = np.array(cv2.boundingRect(dst), dtype=np.float32)
            src_rect = np.array(cv2.boundingRect(src_rect), dtype=np.float32)
            src_rect[2] += src_rect[0]
            src_rect[3] += src_rect[1]
            dst_rect[2] += dst_rect[0]
            dst_rect[3] += dst_rect[1]
            if iou(src_rect, dst_rect) < 0.5:
                M = np.eye(3)

        # aligned = cv2.warpPerspective(current_img, M, (final_width, final_height))
        # cv2.imwrite(os.path.join(path, name + '.png'), aligned)
        reference_img = np.copy(current_img)
        psd2pickle(PSDImage.open(psd_path), pkl_path, {'finishing_size': [final_width, final_height], 'transform': M.tolist()})
        if delete_psd:
            os.remove(psd_path)

    print(args, 'done')


def dataset_psd2pkl(root_dir, n_processes=16, delete_psd=False):
    # converts whole dataset into pkl
    args = [(root, files, delete_psd) for root, dirs, files in os.walk(root_dir)]
    with Pool(n_processes) as p:
        p.map(dataset_psd2pkl_worker, args)


def LinearComposite(tensors, blend_modes, background=1):
    # linearly composite layers since there's no layer-group in the dataset
    b, max_layers, c, h, w = tensors.shape
    b, max_layers, n_modes = blend_modes.shape
    blend_modes = blend_modes.view((b, max_layers, n_modes, 1, 1, 1))
    ret = torch.ones((b, 3, h, w), dtype=torch.float32).to(tensors.device) * background
    for i in range(max_layers):
        alpha = tensors[:, i, 3:, :, :].contiguous()
        src = tensors[:, i, :3, :, :].contiguous()
        src_alpha = src * alpha
        shaded_base = (1.0 - alpha) * ret
        normal = src_alpha + shaded_base
        multiply = src_alpha * ret + shaded_base
        linear_dodge = (src_alpha + ret).clamp(0.0, 1.0)
        screen = 1.0 - (1.0 - ret) * (1.0 - src_alpha)
        blendings = torch.stack((normal, multiply, linear_dodge, screen), 1)
        ret = torch.sum(blend_modes[:, i, :BLEND_DICT[b'padding'], :, :, :] * blendings, 1)
    return ret


def test_loader():
    # dataset_psd2pkl('./debug/dataset/set', n_processes=16, delete_psd=True)
    """
    ml = 16
    dataset = PSDPickleDataset('./debug/dataset/set', max_layers=ml, transform=PSDBasicAugmentation((512, 512)), post_filter=drop_full_post_filter)

    dataset = PSDPreviewPairDataset('./debug/dataset/set')
    cur, last = dataset[79]
    cur = np.uint8(denormalize_img(cur)).transpose((1, 2, 0))
    last = np.uint8(denormalize_img(last)).transpose((1, 2, 0))
    Image.fromarray(cur).save('./debug/cur.png')
    Image.fromarray(last).save('./debug/last.png')

    dataset = PSDPickleDataset('./debug/dataset/set', max_layers=ml, transform=None, post_filter=drop_full_post_filter)
    tensors, bm = dataset.load_pkl_data('./debug/dataset/set/34b447c5f9ae4d4b858475eacd09ce38/1.pkl', False, True, False)
    tensors, bm = torch.tensor(tensors).unsqueeze(0), torch.tensor(bm).unsqueeze(0)
    res = LinearComposite(tensors, bm, background=0.5)
    print(bm)
    print(res.shape)
    res = res[0].permute((1, 2, 0)).detach().cpu().numpy()
    Image.fromarray(np.uint8(res * 255)).convert('RGB').save('./debug/composite.png')
    """


if __name__ == '__main__':
    test_loader()
