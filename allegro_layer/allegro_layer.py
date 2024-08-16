# leap_hand layer for torch
import torch
import trimesh
import os
import numpy as np
import copy
import pytorch_kinematics as pk


import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from layer_asset_utils import save_part_mesh, sample_points_on_mesh, sample_visible_points

BASE_DIR = os.path.split(os.path.abspath(__file__))[0]
# All lengths are in mm and rotations in radians


class AllegroHandLayer(torch.nn.Module):
    def __init__(self, to_mano_frame=True, show_mesh=False, hand_type='right', device='cuda'):
        super().__init__()

        self.show_mesh = show_mesh
        self.to_mano_frame = to_mano_frame
        self.device = device
        self.name = 'allegro_hand'
        self.hand_type = hand_type
        self.finger_num = 4

        urdf_path = os.path.join(BASE_DIR, '../assets/allegro_hand_description_{}.urdf'.format(hand_type))
        self.chain = pk.build_chain_from_urdf(open(urdf_path).read()).to(device=device)

        self.joints_lower = self.chain.low
        self.joints_upper = self.chain.high
        self.joints_mean = (self.joints_lower + self.joints_upper) / 2
        self.joints_range = self.joints_mean - self.joints_lower
        self.joint_names = self.chain.get_joint_parameter_names()
        self.n_dofs = self.chain.n_joints  # only used here for robot hand with no mimic joint

        # print(self.chain.get_links())
        self.link_dict = {}
        for link in self.chain.get_links():
            self.link_dict[link.name] = link.visuals[0].geom_param[0].split('/')[-1]

        # order in palm -> thumb -> index -> middle -> ring [-> pinky(little)]
        self.order_keys = [
            'palm_link',  # palm
            'link_12.0', 'link_13.0', 'link_14.0', 'link_15.0', 'link_15.0_tip',  # thumb
            'link_0.0', 'link_1.0', 'link_2.0', 'link_3.0', 'link_3.0_tip',  # index
            'link_4.0', 'link_5.0', 'link_6.0', 'link_7.0', 'link_7.0_tip',  # middle
            'link_8.0', 'link_9.0', 'link_10.0', 'link_11.0', 'link_11.0_tip'  # ring
        ]

        self.ordered_finger_endeffort = ['palm_link',  'link_15.0_tip', 'link_3.0_tip', 'link_7.0_tip', 'link_11.0_tip']

        # transformation for align the robot hand to mano hand frame, used for
        self.to_mano_transform = torch.eye(4).to(torch.float32).to(device)
        if self.to_mano_frame:
            self.to_mano_transform[:3, :] = torch.tensor([[0, 0, -1, 0],
                                                          [-1, 0, 0, 0],
                                                          [0, 1,  0, 0]])

        self.register_buffer('base_2_world', self.to_mano_transform)

        if not (os.path.exists(os.path.abspath(os.path.dirname(__file__)) + '/../assets/hand_meshes_cvx')
                and os.path.exists(os.path.abspath(os.path.dirname(__file__)) + '/../assets/hand_points')
                and os.path.exists(os.path.abspath(os.path.dirname(__file__)) + '/../assets/hand_composite_points')
                and os.path.exists(os.path.abspath(os.path.dirname(__file__)) + '/../assets/visible_point_indices')
                and os.path.exists(os.path.abspath(os.path.dirname(__file__)) + '/../assets/hand.obj')
                and os.path.exists(os.path.abspath(os.path.dirname(__file__)) + '/../assets/hand_all_zero.obj')
        ):
            # for first time run to generate contact points on the hand, set the self.make_contact_points=True
            self.make_contact_points = True
            self.create_assets()
        else:
            self.make_contact_points = False

        self.meshes = self.load_meshes()
        self.hand_segment_indices, self.hand_finger_indices = self.get_hand_segment_indices()

    def create_assets(self):
        '''
        To create needed assets for the first running.
        Should run before first use.
        '''
        self.to_mano_transform = torch.eye(4).to(torch.float32).to(device)
        pose = torch.from_numpy(np.identity(4)).to(device).reshape(-1, 4, 4).float()
        theta = np.zeros((1, self.n_dofs), dtype=np.float32)

        save_part_mesh()
        sample_points_on_mesh()

        show_mesh = self.show_mesh
        self.show_mesh = True
        self.make_contact_points = True

        self.meshes = self.load_meshes()
        mesh = self.get_forward_hand_mesh(pose, theta)[0]
        parts = mesh.split()

        new_mesh = trimesh.boolean.boolean_manifold(parts, 'union')
        new_mesh.export(os.path.join(BASE_DIR, '../assets/hand.obj'))

        self.show_mesh = True
        self.make_contact_points = False
        self.meshes = self.load_meshes()
        mesh = self.get_forward_hand_mesh(pose, theta)[0]
        mesh.export(os.path.join(BASE_DIR, '../assets/hand_all_zero.obj'))

        self.show_mesh = False
        self.make_contact_points = True
        self.meshes = self.load_meshes()

        self.get_forward_vertices(pose, theta)      # SAMPLE hand_composite_points
        sample_visible_points()

        self.show_mesh = True
        self.make_contact_points = False

        self.to_mano_transform[:3, :] = torch.tensor([[0, 0, -1, 0],
                                                     [-1, 0, 0, 0],
                                                     [0, 1, 0, 0]])
        self.meshes = self.load_meshes()
        mesh = self.get_forward_hand_mesh(pose, theta)[0]
        mesh.export(os.path.join(BASE_DIR, '../assets/hand_to_mano_frame.obj'))

        self.make_contact_points = False
        self.show_mesh = show_mesh

    def load_meshes(self):
        mesh_dir = os.path.dirname(os.path.realpath(__file__)) + "/../assets/hand_meshes/"
        meshes = {}
        for key, value in self.link_dict.items():
            mesh_filepath = os.path.join(mesh_dir, value)
            link_pre_transform = self.chain.find_link(key).visuals[0].offset
            if self.show_mesh:
                mesh = trimesh.load(mesh_filepath)
                if self.make_contact_points:
                    mesh = trimesh.load(mesh_filepath.replace('assets/hand_meshes/', 'assets/hand_meshes_cvx/'))

                verts = link_pre_transform.transform_points(torch.FloatTensor(np.array(mesh.vertices)))

                temp = torch.ones(mesh.vertices.shape[0], 1).float()
                vertex_normals = link_pre_transform.transform_normals(torch.FloatTensor(copy.deepcopy(mesh.vertex_normals)))
                meshes[key] = [
                    torch.cat((verts, temp), dim=-1).to(self.device),
                    mesh.faces,
                    torch.cat((vertex_normals, temp), dim=-1).to(self.device).to(torch.float)
                ]
            else:
                vertex_path = mesh_filepath.replace('hand_meshes', 'hand_points').replace('.stl', '.npy').replace('.STL', '.npy')
                assert os.path.exists(vertex_path)
                points_info = np.load(vertex_path)

                link_pre_transform = self.chain.find_link(key).visuals[0].offset
                if self.make_contact_points:
                    idxs = np.arange(len(points_info))
                else:
                    idxs = np.load(os.path.dirname(os.path.realpath(__file__)) + '/../assets/visible_point_indices/{}.npy'.format(key))

                verts = link_pre_transform.transform_points(torch.FloatTensor(points_info[idxs, :3]))
                # print(key, value, verts.shape)
                vertex_normals = link_pre_transform.transform_normals(torch.FloatTensor(points_info[idxs, 3:6]))

                temp = torch.ones(idxs.shape[0], 1)

                meshes[key] = [
                    torch.cat((verts, temp), dim=-1).to(self.device),
                    torch.zeros([0]),  # no real meaning, just for placeholder
                    torch.cat((vertex_normals, temp), dim=-1).to(torch.float).to(self.device)
                ]

        return meshes

    def get_hand_segment_indices(self):
        hand_segment_indices = {}
        hand_finger_indices = {}
        segment_start = torch.tensor(0, dtype=torch.long, device=self.device)
        finger_start = torch.tensor(0, dtype=torch.long, device=self.device)
        for link_name in self.order_keys:
            end = torch.tensor(self.meshes[link_name][0].shape[0], dtype=torch.long, device=self.device) + segment_start
            hand_segment_indices[link_name] = [segment_start, end]
            if link_name in self.ordered_finger_endeffort:
                hand_finger_indices[link_name] = [finger_start, end]
                finger_start = end.clone()
            segment_start = end.clone()
        return hand_segment_indices, hand_finger_indices

    def forward(self, theta):
        """
        Args:
            theta (Tensor (batch_size x 15)): The degrees of freedom of the Robot hand.
       """
        ret = self.chain.forward_kinematics(theta)
        return ret

    def compute_abnormal_joint_loss(self, theta):
        loss_1 = torch.clamp(theta[:, 4] - theta[:, 0], 0, 1) * 20
        loss_2 = torch.clamp(theta[:, 8] - theta[:, 4], 0, 1) * 20
        loss_3 = torch.abs(theta[:, [2, 3,  6, 7,  10, 11, 14, 15]] - self.joints_mean[[2, 3,  6, 7,  10, 11, 14, 15]].unsqueeze(0)).sum(dim=-1) * 2
        return loss_1 + loss_2 + loss_3

    def get_init_angle(self):
        init_angle = (self.joints_upper - self.joints_lower) / 6.0 + self.joints_lower
        init_angle[0] = 0.1
        init_angle[4] = 0.0
        init_angle[8] = -0.1
        init_angle[12] = 0.8
        return

    def get_hand_mesh(self, pose, ret):
        bs = pose.shape[0]

        meshes = []
        for key in self.order_keys:
            rotmat = ret[key].get_matrix()
            rotmat = torch.matmul(pose, torch.matmul(self.to_mano_transform, rotmat))

            vertices = self.meshes[key][0]
            batch_vertices = torch.matmul(rotmat, vertices.transpose(0, 1)).transpose(1, 2)[..., :3]
            face = self.meshes[key][1]
            sub_meshes = [trimesh.Trimesh(vertices.cpu().numpy(), face) for vertices in batch_vertices]

            meshes.append(sub_meshes)

        hand_meshes = []
        for j in range(bs):
            hand = [meshes[i][j] for i in range(len(meshes))]
            hand_mesh = np.sum(hand)
            hand_meshes.append(hand_mesh)
        return hand_meshes

    def get_forward_hand_mesh(self, pose, theta):
        outputs = self.forward(theta)

        hand_meshes = self.get_hand_mesh(pose, outputs)

        return hand_meshes

    def get_forward_vertices(self, pose, theta):
        outputs = self.forward(theta)

        verts = []
        verts_normal = []

        # for key, item in self.meshes.items():
        for key in self.order_keys:
            rotmat = outputs[key].get_matrix()
            rotmat = torch.matmul(pose, torch.matmul(self.to_mano_transform, rotmat))

            vertices = self.meshes[key][0]
            vertex_normals = self.meshes[key][2]
            batch_vertices = torch.matmul(rotmat, vertices.transpose(0, 1)).transpose(1, 2)[..., :3]
            verts.append(batch_vertices)

            if self.make_contact_points:
                if not os.path.exists('../assets/hand_composite_points'):
                    os.makedirs('../assets/hand_composite_points', exist_ok=True)
                np.save('../assets/hand_composite_points/{}.npy'.format(key),
                        batch_vertices.squeeze().cpu().numpy())
            rotmat[:, :3, 3] *= 0
            batch_vertex_normals = torch.matmul(rotmat, vertex_normals.transpose(0, 1)).transpose(1, 2)[..., :3]
            verts_normal.append(batch_vertex_normals)

        verts = torch.cat(verts, dim=1).contiguous()
        verts_normal = torch.cat(verts_normal, dim=1).contiguous()
        return verts, verts_normal


class AllegroAnchor(torch.nn.Module):
    def __init__(self):
        super().__init__()
        # vert_idx
        vert_idx = np.array([
            # thumb finger
            1854, 2077, 2362, 2303, 2318,
            54, 1361,

            # index finger
            2700, 2826, 3013, 2937, 3051,  # 2324
            113,

            # middle finger
            3275, 3439, 3767, 3654, 3640,
            473, 1294,

            # ring finger
            4048, 4193, 4452, 4389, 4459,
            0, 0,  # place holder

            # little finger
            0, 0, 0, 0, 0,  # place holder

            # # plus
            2931, 2985, 2819, 2831,  # 2440  2463
            3686, 3652, 3485, 3431,
            4330, 4469, 4149, 4278,
            0, 0,  # place holder

        ])
        # vert_idx = np.load(os.path.join(BASE_DIR, 'anchor_idx.npy'))
        self.register_buffer("vert_idx", torch.from_numpy(vert_idx).long())

    def forward(self, vertices):
        """
        vertices: TENSOR[N_BATCH, 4040, 3]
        """
        anchor_pos = vertices[:, self.vert_idx, :]
        return anchor_pos

    def pick_points(self, vertices: np.ndarray):
        import open3d as o3d
        print("")
        print(
            "1) Please pick at least three correspondences using [shift + left click]"
        )
        print("   Press [shift + right click] to undo point picking")
        print("2) Afther picking points, press q for close the window")
        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(vertices)
        vis = o3d.visualization.VisualizerWithEditing()
        vis.create_window()
        vis.add_geometry(pcd)
        vis.run()  # user picks points
        vis.destroy_window()
        print(vis.get_picked_points())
        return vis.get_picked_points()


if __name__ == "__main__":
    device = 'cuda' if torch.cuda.is_available() else 'cpu'

    show_mesh = False
    to_mano_frame = True
    hand = AllegroHandLayer(show_mesh=show_mesh, to_mano_frame=to_mano_frame, device=device)

    pose = torch.from_numpy(np.identity(4)).to(device).reshape(-1, 4, 4).float()
    theta = np.zeros((1, hand.n_dofs), dtype=np.float32)
    theta[0, 0:16] = np.array([0.5, 0, 0, 0,
                               0,   0, 0, 0,
                               -.5, 0, 0, 0,
                               0.8, 0, 0, 0])
    theta = torch.from_numpy(theta).to(device)

    # mesh version
    if show_mesh:
        mesh = hand.get_forward_hand_mesh(pose, theta)[0]
        mesh.show()
    else:
        verts, normals = hand.get_forward_vertices(pose, theta)
        pc = trimesh.PointCloud(verts.squeeze().cpu().numpy(), colors=(0, 255, 255))
        ray_visualize = trimesh.load_path(np.hstack((verts[0].detach().cpu().numpy(),
                                                     verts[0].detach().cpu().numpy() + normals[0].detach().cpu().numpy() * 0.01)).reshape(-1, 2, 3))

        mesh = trimesh.load(os.path.join(BASE_DIR, '../assets/hand_to_mano_frame.obj'))

        anchor_layer = AllegroAnchor()
        # anchor_layer.pick_points(verts.squeeze().cpu().numpy())
        anchors = anchor_layer(verts).squeeze().cpu().numpy()
        pc_anchors = trimesh.PointCloud(anchors, colors=(0, 0, 255))
        ray_visualize = trimesh.load_path(np.hstack((verts[0].detach().cpu().numpy(),
                                                     verts[0].detach().cpu().numpy() + normals[
                                                         0].detach().cpu().numpy() * 0.01)).reshape(-1, 2, 3))

        scene = trimesh.Scene([mesh, pc, pc_anchors, ray_visualize])
        scene.show()

