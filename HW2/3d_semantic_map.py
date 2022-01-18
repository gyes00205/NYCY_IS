"""
Usage: 
python 3d_semantic_map.py --test_scene=Data_collection/first_floor --floor=1

python 3d_semantic_map.py --test_scene=Data_collection/second_floor --floor=2
"""
import numpy as np
import cv2
import argparse
import os
import open3d as o3d
from PIL import Image
import time
import copy
import pandas as pd
import math
from sklearn.neighbors import NearestNeighbors
import gc

def parse_config():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test_scene", default="first_floor")
    parser.add_argument("--floor", required=True)
    parser.add_argument("--use_open3d", default=1)
    # parser.add_argument("--gt", required=True)
    
    return parser.parse_args()

def draw_registration_result(source, target, transformation, paint_uniform_color=True):
    source_temp = copy.deepcopy(source)
    target_temp = copy.deepcopy(target)
    if paint_uniform_color:
        source_temp.paint_uniform_color([1, 0.706, 0])
        target_temp.paint_uniform_color([0, 0.651, 0.929])
    source_temp.transform(transformation)
    o3d.visualization.draw_geometries([source_temp, target_temp])

def preprocess_point_cloud(pcd, voxel_size):
    # print(":: Downsample with a voxel size %.3f." % voxel_size)
    pcd_down = pcd.voxel_down_sample(voxel_size)

    radius_normal = voxel_size * 2
    # print(":: Estimate normal with search radius %.3f." % radius_normal)
    pcd_down.estimate_normals(
        o3d.geometry.KDTreeSearchParamHybrid(radius=radius_normal, max_nn=30))

    radius_feature = voxel_size * 5
    # print(":: Compute FPFH feature with search radius %.3f." % radius_feature)
    pcd_fpfh = o3d.pipelines.registration.compute_fpfh_feature(
        pcd_down,
        o3d.geometry.KDTreeSearchParamHybrid(radius=radius_feature, max_nn=100))
    return pcd_down, pcd_fpfh

def prepare_dataset(source, target, voxel_size):
    source_down, source_fpfh = preprocess_point_cloud(source, voxel_size)
    target_down, target_fpfh = preprocess_point_cloud(target, voxel_size)
    return source, target, source_down, target_down, source_fpfh, target_fpfh

def execute_global_registration(source_down, target_down, source_fpfh,
                                target_fpfh, voxel_size):
    distance_threshold = voxel_size * 1.5
    # print(":: RANSAC registration on downsampled point clouds.")
    # print("   Since the downsampling voxel size is %.3f," % voxel_size)
    # print("   we use a liberal distance threshold %.3f." % distance_threshold)
    result = o3d.pipelines.registration.registration_ransac_based_on_feature_matching(
        source_down, target_down, source_fpfh, target_fpfh, True,
        distance_threshold,
        o3d.pipelines.registration.TransformationEstimationPointToPoint(False),
        3, [
            o3d.pipelines.registration.CorrespondenceCheckerBasedOnEdgeLength(
                0.9),
            o3d.pipelines.registration.CorrespondenceCheckerBasedOnDistance(
                distance_threshold)
        ], o3d.pipelines.registration.RANSACConvergenceCriteria(100000, 0.999))
    return result

def execute_fast_global_registration(source_down, target_down, source_fpfh,
                                     target_fpfh, voxel_size):
    distance_threshold = voxel_size * 0.5
    # print(":: Apply fast global registration with distance threshold %.3f" \
    #         % distance_threshold)
    result = o3d.pipelines.registration.registration_fast_based_on_feature_matching(
        source_down, target_down, source_fpfh, target_fpfh,
        o3d.pipelines.registration.FastGlobalRegistrationOption(
            maximum_correspondence_distance=distance_threshold))
    return result

def depth_image_to_point_cloud(rgb_img, depth_img, intrinsic_mtx):
    rgb = cv2.imread(rgb_img)[:,:,[2,1,0]].reshape((-1,3))
    depth = cv2.imread(depth_img, 0)
    depth_scale = 1000.0
    fx, fy, cx, cy = intrinsic_mtx[0,0], intrinsic_mtx[1,1], intrinsic_mtx[0,2], intrinsic_mtx[1,2]

    x = np.zeros(depth.shape)
    y = np.zeros(depth.shape)
    z = depth / depth_scale
    for i in range(x.shape[1]):
        x[:,i] = i
    x = ((x - x.shape[1] / 2) * z) / fx
    for i in range(y.shape[0]):
        y[i,:] = i
    y = ((y - y.shape[0] / 2) * z) / fy
    
    x, y, z = x.reshape((-1,1)), y.reshape((-1,1)), z.reshape((-1,1))
    r, g, b = rgb[:,0].reshape((-1,1)), rgb[:,1].reshape((-1,1)), rgb[:,2].reshape((-1,1))
    points = np.concatenate((x, -y, -z), axis=1)
    colors = np.concatenate((r, g, b), axis=1) / 255.0
    
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points)
    pcd.colors = o3d.utility.Vector3dVector(colors)
    return pcd

def local_icp_algorithm(pcd1, pcd2, trans_init, threshold):
    # print("Apply point-to-point ICP")
    reg_p2p = o3d.pipelines.registration.registration_icp(
        pcd1, pcd2, threshold, trans_init,
        o3d.pipelines.registration.TransformationEstimationPointToPoint(),
        o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=100))
    # draw_registration_result(pcd1, pcd2, reg_p2p.transformation, paint_uniform_color=False)
    return reg_p2p.transformation

def best_fit_transform(source, target):
    '''
    Calculates the least-squares best-fit transform that maps corresponding points A to B in m spatial dimensions
    Input:
      A: Nxm numpy array of corresponding points
      B: Nxm numpy array of corresponding points
    Returns:
      T: (m+1)x(m+1) homogeneous transformation matrix that maps A on to B
      R: mxm rotation matrix
      t: mx1 translation vector
    '''

    assert source.shape == target.shape

    # get number of dimensions
    m = source.shape[1]

    # translate points to their centroids
    centroid_source = np.mean(source, axis=0)
    centroid_target = np.mean(target, axis=0)
    Source = source - centroid_source
    Target = target - centroid_target
    # print(Source.shape)

    # rotation matrix
    W = Target.T @ Source # mxN @ Nxm
    U, S, Vt = np.linalg.svd(W)
    R = U @ Vt
    # print(R.shape)

    # special reflection case
    if np.linalg.det(R) < 0:
       Vt[m-1,:] *= -1
       R = U @ Vt

    # translation
    t = centroid_target.T - R @ centroid_source.T

    # homogeneous transformation
    T = np.identity(m+1)
    T[:m, :m] = R
    T[:m, m] = t

    return T

def local_icp_algorithm_own(pcd1, pcd2, trans_init, threshold):
    # pass
    max_iterations = 100
    source = copy.deepcopy(pcd1)
    target = copy.deepcopy(pcd2)
    
    source = np.asarray(source.points)
    target = np.asarray(target.points)

    m = source.shape[1]

    source_temp = np.ones((m+1, source.shape[0]))
    target_temp = np.ones((m+1, target.shape[0]))
    
    source_temp[:m,:] = np.copy(source.T)
    target_temp[:m,:] = np.copy(target.T)
    
    source_temp = trans_init @ source_temp

    prev_error = 0
    
    for i in range(max_iterations):
        # find the nearest neighbours between the current source and destination points
        neigh = NearestNeighbors(n_neighbors=1, radius=threshold, algorithm='auto')
        neigh.fit(target_temp[:m,:].T)
        distances, indices = neigh.kneighbors(source_temp[:m,:].T)
        indices = indices.reshape(-1)
        distances = distances.reshape(-1)
        valid = distances < threshold
        source_temp = source_temp[:,valid]
        target_temp = target_temp[:,indices]
        target_temp = target_temp[:,valid]
        source = source[valid,:]
        # compute the transformation between the current source and nearest destination points
        T = best_fit_transform(source_temp[:m,:].T, target_temp[:m,:].T)

        # update the current source
        source_temp = T @ source_temp

        # check error
        mean_error = np.sum(distances) / distances.size
        # print(mean_error)
        if abs(prev_error - mean_error) < 0.0001:
            break
        prev_error = mean_error

    # calculcate final tranformation
    T = best_fit_transform(source, source_temp[:m,:].T)

    return T

def calculate_error_distance(line1, line2):
    p1, p2 = np.asarray(line1.points), np.asarray(line2.points)
    error_dst = ((p1[:,0] - p2[:,0])**2 + (p1[:,1] - p2[:,1])**2 + (p1[:,2] - p2[:,2])**2)**0.5
    return np.sum(error_dst)
 
def euler_from_quaternion(w, x, y, z):
    """
    Convert a quaternion into euler angles (roll, pitch, yaw)
    roll is rotation around x in radians (counterclockwise)
    pitch is rotation around y in radians (counterclockwise)
    yaw is rotation around z in radians (counterclockwise)
    """
    t0 = +2.0 * (w * x + y * z)
    t1 = +1.0 - 2.0 * (x * x + y * y)
    roll_x = math.atan2(t0, t1)
    
    t2 = +2.0 * (w * y - z * x)
    t2 = +1.0 if t2 > +1.0 else t2
    t2 = -1.0 if t2 < -1.0 else t2
    pitch_y = math.asin(t2)
    
    t3 = +2.0 * (w * z + x * y)
    t4 = +1.0 - 2.0 * (y * y + z * z)
    yaw_z = math.atan2(t3, t4)
    rz = np.array([[math.cos(yaw_z), -math.sin(yaw_z), 0.0],
                   [math.sin(yaw_z), math.cos(yaw_z), 0.0],
                   [0.0, 0.0, 1.0]])
    ry = np.array([[math.cos(pitch_y), 0.0, math.sin(pitch_y)],
                   [0.0, 1.0, 0.0],
                   [-math.sin(pitch_y), 0.0, math.cos(pitch_y)]])
    rx = np.array([[1.0, 0.0, 0.0],
                   [0.0, math.cos(roll_x), -math.sin(roll_x)],
                   [0.0, math.sin(roll_x), math.cos(roll_x)]])
    
    R = rz @ (ry @ rx)
    return R # in radians


def custom_voxel_down(pcd, voxel_size):
    points = np.asarray(pcd.points)
    colors = np.asarray(pcd.colors)
    min_bound = pcd.get_min_bound()
    max_bound = pcd.get_max_bound()
    points_mean_list = []
    colors_mode_list = []
    range_x = np.arange(min_bound[0].item(), max_bound[0].item()+voxel_size, voxel_size)
    range_y = np.arange(min_bound[1].item(), max_bound[1].item()+voxel_size, voxel_size)
    range_z = np.arange(min_bound[2].item(), max_bound[2].item()+voxel_size, voxel_size)
    x, y, z = points[:,0].reshape((-1,1)), points[:,1].reshape((-1,1)), points[:,2].reshape((-1,1))
    # valid_x = [((x >= i) & (x < i+voxel_size)).reshape(-1) for i in range_x]
    valid_x = [np.logical_and(x>=i, x<i+voxel_size).reshape(-1) for i in range_x]
    # valid_y = [((y >= j) & (y < j+voxel_size)).reshape(-1) for j in range_y]
    valid_y = [np.logical_and(y>=j, y<j+voxel_size).reshape(-1) for j in range_y]
    # valid_z = [((z >= k) & (z < k+voxel_size)).reshape(-1) for k in range_z]
    valid_z = [np.logical_and(z>=k, z<k+voxel_size).reshape(-1) for k in range_z]

    for i in valid_x:
        if points[i].shape[0] == 0:
            continue
        for j in valid_y:
            valid_ij = np.logical_and(i, j)
            if points[valid_ij].shape[0] == 0:
                continue
            for k in valid_z:
                valid = np.logical_and(valid_ij, k)
                if points[valid].shape[0]:
                    xyz_mean = np.mean(points[valid], axis=0)
                    _, counts = np.unique(colors[valid], return_counts=True, axis=0)
                    index = np.argmax(counts)
                    colors_mode = colors[valid][index]
                    points_mean_list.append(xyz_mean)
                    colors_mode_list.append(colors_mode)
    pcd_down = o3d.geometry.PointCloud()
    pcd_down.points = o3d.utility.Vector3dVector(np.array(points_mean_list))
    pcd_down.colors = o3d.utility.Vector3dVector(np.array(colors_mode_list))
    return pcd_down

if __name__ == '__main__':
    args = parse_config()
    use_open3d = int(args.use_open3d)
    rgb_path = os.path.join(args.test_scene, 'dataset0_pred')
    depth_path = os.path.join(args.test_scene, 'depth')
    num_files = len(os.listdir(rgb_path))
    intrinsic_mtx = np.array([[256.0,0.0,256.0],
                              [0.0,256.0,256.0],
                              [0.0,0.0,1.0]])
    voxel_size = 0.002
    threshold = voxel_size * 0.4
    trans_mtx_list = []
    pcd_list = []
    for i in range(num_files,0,-1):
        pcd = depth_image_to_point_cloud(os.path.join(rgb_path, str(i)+'.png'),
                                      os.path.join(depth_path, str(i)+'.png'),
                                      intrinsic_mtx)
        pcd_list.append(pcd)
    num_pcd = len(pcd_list)
    # print(np.asarray(pcd_list[1].points))

    for i in range(num_pcd-1):
        print(f'{num_pcd-i}->{num_pcd-i-1}:')
        source = pcd_list[i]
        target = pcd_list[i+1]
        source, target, source_down, target_down, source_fpfh, target_fpfh = \
            prepare_dataset(source, target, voxel_size)
        start = time.time()
        result_ransac = execute_global_registration(source_down, target_down,
                                                    source_fpfh, target_fpfh,
                                                    voxel_size)
        # print("Global registration took %.3f sec.\n" % (time.time() - start))
        # print(result_ransac.transformation)
        # draw_registration_result(source_down, target_down, result_ransac.transformation)    
        if use_open3d:
            transformation = local_icp_algorithm(source_down, target_down, result_ransac.transformation, threshold)
        else:
            transformation = local_icp_algorithm_own(source_down, target_down, result_ransac.transformation, threshold)
        # draw_registration_result(source, target, transformation, paint_uniform_color=False)
        # print(result_ransac.transformation)
        trans_mtx_list.append(transformation)
        
    
    for i in range(len(trans_mtx_list)-2, -1, -1):
        trans_mtx_list[i] = trans_mtx_list[i+1] @ trans_mtx_list[i]
        
    for i in range(len(trans_mtx_list)):
        pcd_list[i].transform(trans_mtx_list[i])


    roof_threshold = 0.01 if args.floor == 1 else 0.001  
    for i in range(num_pcd):
        points = np.asarray(pcd_list[i].points)
        colors = np.asarray(pcd_list[i].colors)
        x, y, z = points[:,0].reshape((-1,1)), points[:,1].reshape((-1,1)), points[:,2].reshape((-1,1))
        r, g, b = colors[:,0].reshape((-1,1)), colors[:,1].reshape((-1,1)), colors[:,2].reshape((-1,1))
        # print(np.max(y))
        # y < 0.01 for 1st floor, y < 0.001 for 2nd floor 
        valid = (y < roof_threshold).reshape(-1)
        x, y, z = x[valid], y[valid], z[valid]
        r, g, b = r[valid], g[valid], b[valid]
        points = np.concatenate((x,y,z), axis=1)
        colors = np.concatenate((r,g,b), axis=1)
        pcd_list[i].points = o3d.utility.Vector3dVector(points)
        pcd_list[i].colors = o3d.utility.Vector3dVector(colors)

    # pcd_total = o3d.geometry.PointCloud()
    for i in range(num_pcd):
        print(i)
        # pcd_total += pcd_list[i]
        pcd_list[i] = custom_voxel_down(pcd_list[i], 0.003)
    # pcd_total = custom_voxel_down(pcd_total, 0.002)
    # pcd_list.append(line_estimated)
    # pcd_list.append(line_gt)
    o3d.visualization.draw_geometries(pcd_list)
    # o3d.io.write_point_cloud(args.test_scene+'.pcd', sum(pcd_list))
    del pcd_list
    gc.collect()

    rgb_path = os.path.join(args.test_scene, 'other_pred')
    pcd_list = []
    for i in range(num_files,0,-1):
        pcd = depth_image_to_point_cloud(os.path.join(rgb_path, str(i)+'.png'),
                                      os.path.join(depth_path, str(i)+'.png'),
                                      intrinsic_mtx)
        pcd_list.append(pcd)

    for i in range(len(trans_mtx_list)):
        pcd_list[i].transform(trans_mtx_list[i])

    for i in range(num_pcd):
        points = np.asarray(pcd_list[i].points)
        colors = np.asarray(pcd_list[i].colors)
        x, y, z = points[:,0].reshape((-1,1)), points[:,1].reshape((-1,1)), points[:,2].reshape((-1,1))
        r, g, b = colors[:,0].reshape((-1,1)), colors[:,1].reshape((-1,1)), colors[:,2].reshape((-1,1))
        # print(np.max(y))
        # y < 0.01 for 1st floor, y < 0.001 for 2nd floor 
        valid = (y < roof_threshold).reshape(-1)
        x, y, z = x[valid], y[valid], z[valid]
        r, g, b = r[valid], g[valid], b[valid]
        points = np.concatenate((x,y,z), axis=1)
        colors = np.concatenate((r,g,b), axis=1)
        pcd_list[i].points = o3d.utility.Vector3dVector(points)
        pcd_list[i].colors = o3d.utility.Vector3dVector(colors)
    for i in range(num_pcd):
        print(i)
        pcd_list[i] = custom_voxel_down(pcd_list[i], 0.003)
    o3d.visualization.draw_geometries(pcd_list)
    del pcd_list
    gc.collect()