import os
import cv2
import json
import argparse


def odgt(img_path):
    seg_path = img_path.replace('images','annotations')
    seg_path = seg_path.replace('.jpg','.png')
    
    if os.path.exists(seg_path):
        img = cv2.imread(img_path)
        h, w, _ = img.shape

        odgt_dic = {}
        odgt_dic["fpath_img"] = img_path
        odgt_dic["fpath_segm"] = seg_path
        odgt_dic["width"] = h
        odgt_dic["height"] = w
        return odgt_dic
    else:
        # print('the corresponded annotation does not exist')
        # print(img_path)
        return None


def parse_config():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, type=str)
    return parser.parse_args()    


if __name__ == "__main__":
    args = parse_config()
    modes = ['train','val','test']
    saves = ['training.odgt', 'validation.odgt','test.odgt'] # customized

    for i, mode in enumerate(modes):
        save = saves[i]
        dir_path = os.path.join(args.dataset, 'images', mode)
        img_list = os.listdir(dir_path)
        img_list.sort()
        img_list = [os.path.join(dir_path, img) for img in img_list]

        with open(f'{args.dataset}/{save}', mode='wt', encoding='utf-8') as myodgt:
            for i, img in enumerate(img_list):
                a_odgt = odgt(img)
                if a_odgt is not None:
                    myodgt.write(f'{json.dumps(a_odgt)}\n')
