
import os
import xml.etree.ElementTree as ET
import bpy
from . import bone_utils, mot_writer, pole_utils, rig_utils

def read_data():
    addon_dir = os.path.dirname(os.path.abspath(__file__))
    xml_file = os.path.join(addon_dir, "data", "BoneDataTypes.xml")
    tree = ET.parse(xml_file)
    root = tree.getroot()
    
    # Bones
    bones_data = []
    for elem in root.findall("Bones/Bone"):
        item_data = {
            "Name": elem.find("Name").text,
            "Type": elem.find("Type").text,
            "IKTarget": elem.find("IKTarget").text if elem.find("IKTarget") is not None else None
        }
        bones_data.append(item_data)
    
    # BoneInfo
    bone_info = []
    for elem in root.findall("BoneInfo/id"):
        bone_info.append(int(elem.text))
    
    return bones_data, bone_info

def collect_transformations(object_name, frame_start, frame_end):
    # Drive the elbow poles from the same frame the sample is taken on.  Relying on
    # frame_change_post for this does not work: handlers restored from a .blend are never run, so
    # the poles export whatever position the template was saved at - a fixed point in space that
    # only looks like an animated channel because its parent bone moves.
    def update_poles(frame):
        problems = pole_utils.drive_scene_poles(bpy.context.scene)
        if problems:
            raise RuntimeError("elbow poles are not driven, refusing to export: %s"                               % "; ".join(problems))

    transformations = bone_utils.collect_bone_transformations(
        object_name, frame_start, frame_end, update_poles)
    return transformations


def process_bones(bones_data, armature_name, bone_transformations, decimals, scale_keys, frame_start):
    export_data = []
    
    for bone in bones_data:
        keysets = bone_utils.handle_bone(
            bone,
            bone_transformations,
            armature_name,
            decimals,
            scale_keys,
            frame_start
        )
        export_data.extend(keysets)

    export_data.append(None)
    return export_data

def export_motion(armature, filepath, decimals, scale_keys):
    try:
        bones_data, bone_info = read_data()

        for note in rig_utils.apply_spine_copies(armature):
            print("Spine mapping: %s" % note)

        frame_start = bpy.context.scene.frame_start
        frame_end = bpy.context.scene.frame_end
        frame_count = int(((frame_end - frame_start) * scale_keys) + 1) 

        bone_transformations = collect_transformations(armature.name, frame_start, frame_end)

        export_data = process_bones(
            bones_data, armature.name, bone_transformations, decimals, scale_keys, frame_start
        )

        mot_writer.write_mot_bin(filepath, export_data, bone_info, frame_count)

        print("Export completed successfully.")
        return True

    except Exception as e:
        print(f"Error: {e}")
        return False
