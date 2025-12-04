import numpy as np
import cv2
import copy

def get_image_blending(image,face,face_box,mask_array,crop_box):
    body = image
    x, y, x1, y1 = face_box
    x_s, y_s, x_e, y_e = crop_box
    face_large = copy.deepcopy(body[y_s:y_e, x_s:x_e])
    face_large[y-y_s:y1-y_s, x-x_s:x1-x_s]=face

    target_region = body[y_s:y_e, x_s:x_e]
    if face_large.shape != target_region.shape:
        face_large = cv2.resize(face_large, (target_region.shape[1], target_region.shape[0]))
    body[y_s:y_e, x_s:x_e] = face_large

    return body