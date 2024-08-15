from urdfpy import URDF

robot = URDF.load('./allegro_hand_description_right.urdf')
cfg = {}
robot.show(cfg=cfg, use_collision=False)


# import os
#
# import trimesh
#
# for root, dirs, files in os.walk('./hand_meshes'):
#     for filename in files:
#         filepath = os.path.join(root, filename)
#         mesh = trimesh.load(filepath)
#         mesh.show()
#         new_filepath = filepath.replace('.STL', '.stl')
#         mesh.export(new_filepath)
