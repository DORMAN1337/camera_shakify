#====================== BEGIN GPL LICENSE BLOCK ======================
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 3
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
#======================= END GPL LICENSE BLOCK ========================

bl_info = {
    "name": "Camera Shakify",
    "version": (0, 5, 0),
    "author": "Nathan Vegdahl, Ian Hubert",
    "blender": (4, 4, 0),
    "description": "Add captured camera shake/wobble to your cameras",
    "location": "Camera properties",
    # "doc_url": "",
    "category": "Animation",
}

import re
import math
import os
import json

import bpy
from bpy.types import Camera, Context
from .action_utils import action_to_python_data_text, ensure_shake_in_action, action_slot_frame_range, ensure_action
from .shake_data import (
    SHAKE_LIST, BUILTIN_KEYS,
    get_all_presets, add_user_preset, remove_user_preset,
    is_builtin, load_user_presets,
    get_display_name, set_display_name, reset_display_names,
)
from .farm_script import ensure_farm_script


# Note: the ".v#" number at the end is *not* the addon version.  This number is
# incremented when the way shakes are constructed changes to prevent
# compatibility problems, and generally spans multiple addon versions.
BASE_NAME = "CameraShakify.v3"
ACTION_NAME = BASE_NAME + " Shakes"
COLLECTION_NAME = BASE_NAME

# Note: the addon used to be called "Camera Wobble" before it was publicly
# released, and had a "v1" and "v2" base name under that name.  We don't include
# those here because those versions of the addon were only ever used internally
# by Ian, and there should be no files that exist anymore that use those base
# names.  But that's why there's also no "CameraShakify.v1", because that never
# existed.
BASE_NAMES_OLD = ["CameraShakify.v2"]

# Maximum values of our per-camera scaling/influence properties.
INFLUENCE_MAX = 4.0
SCALE_MAX = 100.0

# The maximum supported world unit scale.
UNIT_SCALE_MAX = 1000.0


#========================================================


class CameraShakifyPanel(bpy.types.Panel):
    """Add shake to your Cameras."""
    bl_label = "Camera Shakify"
    bl_idname = "DATA_PT_camera_shakify"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"

    @classmethod
    def poll(cls, context):
        return context.active_object.type == 'CAMERA'

    def draw(self, context):
        wm = context.window_manager
        layout = self.layout

        camera = context.active_object

        row = layout.row()
        row.template_list(
            listtype_name="OBJECT_UL_camera_shake_items",
            list_id="Camera Shakes",
            dataptr=camera,
            propname="camera_shakes",
            active_dataptr=camera,
            active_propname="camera_shakes_active_index",
        )
        col = row.column()
        col.operator("object.camera_shake_add", text="", icon='ADD')
        col.operator("object.camera_shake_remove", text="", icon='REMOVE')
        col.operator("object.camera_shake_move", text="", icon='TRIA_UP').type = 'UP'
        col.operator("object.camera_shake_move", text="", icon='TRIA_DOWN').type = 'DOWN'

        if camera.camera_shakes_active_index < len(camera.camera_shakes):
            shake = camera.camera_shakes[camera.camera_shakes_active_index]
            row = layout.row()
            col = row.column(align=True)
            col.alignment = 'RIGHT'
            col.use_property_split = True
            col.prop(shake, "shake_type", text="Shake")
            col.separator()
            col.prop(shake, "influence", slider=True)
            col.separator()
            col.prop(shake, "scale")
            col.separator()
            col.prop(shake, "use_location")
            col.prop(shake, "use_rotation")
            col.separator()
            col.prop(shake, "use_manual_timing")
            if shake.use_manual_timing:
                col.prop(shake, "time")
            else:
                col.prop(shake, "speed")
                col.prop(shake, "offset")
            col.separator()
            col.prop(shake, "use_loop_range")
            col.separator()

        col.separator(factor=2.0)

        row = layout.row()
        row.alignment = 'LEFT'
        header_text = "Misc Utilities"
        if wm.camera_shake_show_utils:
            row.prop(wm, "camera_shake_show_utils", icon="DISCLOSURE_TRI_DOWN", text=header_text, expand=False, emboss=False)
        else:
            row.prop(wm, "camera_shake_show_utils", icon="DISCLOSURE_TRI_RIGHT", text=header_text, emboss=False)
        row.separator_spacer()

        col = layout.column()
        if wm.camera_shake_show_utils:
            col.operator("wm.camera_shakify_import_colmap", icon='IMPORT')
            col.separator()
            col.operator("object.camera_shakes_fix_global")
            col.operator("wm.camera_shakify_prep_file_for_farm")


class OBJECT_UL_camera_shake_items(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        ob = data
        # draw_item must handle the three layout types... Usually 'DEFAULT' and 'COMPACT' can share the same code.
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            col = layout.column()
            col.label(
                text=get_display_name(item.shake_type),
                icon='FCURVE_SNAPSHOT',
            )

            col = layout.column()
            col.alignment = 'RIGHT'
            col.prop(item, "influence", text="", expand=False, slider=True, emboss=False)
        # 'GRID' layout type should be as compact as possible (typically a single icon!).
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="", icon_value=icon)


#========================================================

def loopify_data(data, scene_len, blend_ratio=0.15):
    """Truncate to scene_len frames, crossfade the last N% toward vals[0].
    Returns scene_len keyframes where last ≈ first for seamless loop.
    blend_ratio defaults to 0.15 (15% of scene_len)."""
    blend_count = max(1, min(int(scene_len * blend_ratio), scene_len - 1))
    result = {}
    for channel, keyframes in data.items():
        keyframes.sort(key=lambda x: x[0])
        vals = [v for _, v in keyframes]
        shake_len = len(vals)
        if shake_len < 2:
            wrapped = [vals[i % shake_len] for i in range(scene_len)]
            result[channel] = [[j, v] for j, v in enumerate(wrapped)]
            continue
        # Apply loop_fix on raw data so pre-loop target for last frame = vals[0]
        vals[-1] = vals[0]
        # Wrap shake data to fill scene range
        scene_vals = [vals[i % shake_len] for i in range(scene_len)]
        orig_vals = list(scene_vals)
        # Crossfade last blend_count frames toward the frame that precedes
        # keyframe[0] in the circular shake (shake_len - blend_count + i)
        for i in range(blend_count):
            idx = scene_len - blend_count + i
            t = (i + 1) / blend_count
            cos_t = (1 - math.cos(t * math.pi)) / 2
            pre_loop = (shake_len - blend_count + i) % shake_len
            scene_vals[idx] = (1 - cos_t) * orig_vals[idx] + cos_t * vals[pre_loop]
        # last frame after crossfade = vals[shake_len-1] = vals[0] (loop_fix)
        result[channel] = [[j, v] for j, v in enumerate(scene_vals)]
    return result


# Creates a camera shake setup for the given camera and
# shake item index, using the given collection to store
# shake empties.
def build_single_shake(camera, shake_item_index, collection, context):
    shake = camera.camera_shakes[shake_item_index]
    presets = get_all_presets()
    shake_data = presets[shake.shake_type]

    shake_name = shake.shake_type.lower()
    shake_object_name = BASE_NAME + "_" + camera.name + "_" + str(shake_item_index)

    # Build channel data.
    channels = dict(shake_data[2])

    # Crossfade shake tail into head for seamless loop at scene range.
    if shake.use_loop_range:
        scene_len = context.scene.frame_end - context.scene.frame_start + 1
        if scene_len >= 2:
            channels = loopify_data(channels, scene_len)

    # Ensure the needed action and shake slot exist.
    action = ensure_action(ACTION_NAME)
    slot = ensure_shake_in_action(
        shake_name,
        action,
        channels,
        INFLUENCE_MAX,
        INFLUENCE_MAX * SCALE_MAX * UNIT_SCALE_MAX
    )
    # Ensure the needed shake object exists.
    shake_object = None
    if shake_object_name in bpy.data.objects:
        shake_object = bpy.data.objects[shake_object_name]
    else:
        shake_object = bpy.data.objects.new(shake_object_name, None)

    # Make sure the shake object is linked into our collection.
    if shake_object.name not in collection.objects:
        collection.objects.link(shake_object)

    #----------------
    # Set up the constraints and drivers on the shake object.
    #----------------

    # Clear out all constraints and drivers, and fetch animation data block.
    shake_object.constraints.clear()
    shake_object.animation_data_clear()
    anim_data = shake_object.animation_data_create()

    # Some weird gymnastics needed because of a Blender bug.
    # Without first assigning an action to the animation data,
    # then on a fresh scene we won't be able to assign an action
    # to the action constraint (below).
    anim_data.action = action
    anim_data.action = None
    shake_object.location = (0,0,0)
    shake_object.rotation_euler = (0,0,0)
    shake_object.rotation_quaternion = (0,0,0,0)
    shake_object.rotation_axis_angle = (0,0,0,0)
    shake_object.scale = (1,1,1)

    # Get action info for calculations below.
    shake_fps = shake_data[1]
    shake_range = action_slot_frame_range(action, slot)
    shake_length = shake_range[1] - shake_range[0] + 1

    # Create the action constraint.
    constraint = shake_object.constraints.new('ACTION')
    constraint.use_eval_time = True
    constraint.mix_mode = 'BEFORE'
    constraint.action = action
    constraint.action_slot = slot
    constraint.frame_start = shake_range[0]
    constraint.frame_end = shake_range[1] + 1

    # Create the driver for the constraint's eval time.
    driver = constraint.driver_add("eval_time").driver
    driver.type = 'SCRIPTED'
    fps_factor = 1.0 / ((context.scene.render.fps / context.scene.render.fps_base) / shake_fps)
    rate = fps_factor / shake_length
    norm_expr = "((time if manual else ((-frame_offset + frame) * speed)) * {}) % 1.0".format(rate)
    loop_expr = "((time if manual else ((frame - scene_start + frame_offset) * speed)) / (scene_end - scene_start + 1)) % 1.0"
    driver.expression = "{} if use_loop_range else {}".format(loop_expr, norm_expr)

    manual_timing_var = driver.variables.new()
    manual_timing_var.name = "manual"
    manual_timing_var.type = 'SINGLE_PROP'
    manual_timing_var.targets[0].id_type = 'OBJECT'
    manual_timing_var.targets[0].id = camera
    manual_timing_var.targets[0].data_path = 'camera_shakes[{}].use_manual_timing'.format(shake_item_index)

    time_var = driver.variables.new()
    time_var.name = "time"
    time_var.type = 'SINGLE_PROP'
    time_var.targets[0].id_type = 'OBJECT'
    time_var.targets[0].id = camera
    time_var.targets[0].data_path = 'camera_shakes[{}].time'.format(shake_item_index)

    speed_var = driver.variables.new()
    speed_var.name = "speed"
    speed_var.type = 'SINGLE_PROP'
    speed_var.targets[0].id_type = 'OBJECT'
    speed_var.targets[0].id = camera
    speed_var.targets[0].data_path = 'camera_shakes[{}].speed'.format(shake_item_index)

    offset_var = driver.variables.new()
    offset_var.name = "frame_offset"
    offset_var.type = 'SINGLE_PROP'
    offset_var.targets[0].id_type = 'OBJECT'
    offset_var.targets[0].id = camera
    offset_var.targets[0].data_path = 'camera_shakes[{}].offset'.format(shake_item_index)

    loop_var = driver.variables.new()
    loop_var.name = "use_loop_range"
    loop_var.type = 'SINGLE_PROP'
    loop_var.targets[0].id_type = 'OBJECT'
    loop_var.targets[0].id = camera
    loop_var.targets[0].data_path = 'camera_shakes[{}].use_loop_range'.format(shake_item_index)

    scene_start_var = driver.variables.new()
    scene_start_var.name = "scene_start"
    scene_start_var.type = 'SINGLE_PROP'
    scene_start_var.targets[0].id_type = 'SCENE'
    scene_start_var.targets[0].id = context.scene
    scene_start_var.targets[0].data_path = 'frame_start'

    scene_end_var = driver.variables.new()
    scene_end_var.name = "scene_end"
    scene_end_var.type = 'SINGLE_PROP'
    scene_end_var.targets[0].id_type = 'SCENE'
    scene_end_var.targets[0].id = context.scene
    scene_end_var.targets[0].data_path = 'frame_end'

    #----------------
    # Set up the constraints and drivers on the camera object.
    #----------------

    loc_constraint_name = BASE_NAME + "_loc_" + str(shake_item_index)
    rot_constraint_name = BASE_NAME + "_rot_" + str(shake_item_index)

    # Create the new constraints.
    loc_constraint = camera.constraints.new(type='COPY_LOCATION')
    rot_constraint = camera.constraints.new(type='COPY_ROTATION')
    loc_constraint.name = loc_constraint_name
    rot_constraint.name = rot_constraint_name
    loc_constraint.show_expanded = False
    rot_constraint.show_expanded = False

    # Set up location constraint.
    loc_constraint.target = shake_object
    loc_constraint.target_space = 'WORLD'
    loc_constraint.owner_space = 'LOCAL'
    loc_constraint.use_offset = True

    # Set up rotation constraint.
    rot_constraint.target = shake_object
    rot_constraint.target_space = 'WORLD'
    rot_constraint.owner_space = 'LOCAL'
    rot_constraint.mix_mode = 'AFTER'

    # Set up the location constraint driver.
    #
    # Note: we clear the keyframes from the driver's fcurve to dodge some
    # small-value rounding that Blender does internally when evaluating fcurves.
    # This way the driver expression evaluation gets used directly, without any
    # intermediate steps that might interfere.
    fcurve = loc_constraint.driver_add("influence")
    fcurve.keyframe_points.clear()
    driver = fcurve.driver
    driver.type = 'SCRIPTED'
    driver.expression = "{} * influence * location_scale / unit_scale * int(use_loc)".format(1.0 / (UNIT_SCALE_MAX * INFLUENCE_MAX * SCALE_MAX))
    if "influence" not in driver.variables:
        var = driver.variables.new()
        var.name = "influence"
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'OBJECT'
        var.targets[0].id = camera
        var.targets[0].data_path = 'camera_shakes[{}].influence'.format(shake_item_index)
    if "location_scale" not in driver.variables:
        var = driver.variables.new()
        var.name = "location_scale"
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'OBJECT'
        var.targets[0].id = camera
        var.targets[0].data_path = 'camera_shakes[{}].scale'.format(shake_item_index)
    if "unit_scale" not in driver.variables:
        var = driver.variables.new()
        var.name = "unit_scale"
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'SCENE'
        var.targets[0].id = context.scene
        var.targets[0].data_path ='unit_settings.scale_length'
    if "use_loc" not in driver.variables:
        var = driver.variables.new()
        var.name = "use_loc"
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'OBJECT'
        var.targets[0].id = camera
        var.targets[0].data_path = 'camera_shakes[{}].use_location'.format(shake_item_index)

    # Set up the rotation constraint driver.
    #
    # Note: see further-above note for why we clear the keyframes here.
    fcurve = rot_constraint.driver_add("influence")
    fcurve.keyframe_points.clear()
    driver = fcurve.driver
    driver.type = 'SCRIPTED'
    driver.expression = "influence * {} * int(use_rot)".format(1.0 / INFLUENCE_MAX)
    if "influence" not in driver.variables:
        var = driver.variables.new()
        var.name = "influence"
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'OBJECT'
        var.targets[0].id = camera
        var.targets[0].data_path = 'camera_shakes[{}].influence'.format(shake_item_index)
    if "use_rot" not in driver.variables:
        var = driver.variables.new()
        var.name = "use_rot"
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'OBJECT'
        var.targets[0].id = camera
        var.targets[0].data_path = 'camera_shakes[{}].use_rotation'.format(shake_item_index)


# Only for use in rebuilding camera shakes, to ensure that constraints, etc.
# from previous Camera Shakify versions get removed.
def starts_with_any_base_name(text):
    base_names = BASE_NAMES_OLD + [BASE_NAME]

    for base_name in base_names:
        if text.startswith(base_name):
            return True

    return False

# Ensure that our camera shakify collection exists and fetch it.
def ensure_camera_shakify_collection(context):
    if COLLECTION_NAME in context.scene.collection.children and context.scene.collection.children[COLLECTION_NAME].library == None:
        return context.scene.collection.children[COLLECTION_NAME]

    # Get the collection.
    #
    # The song-and-dance here is to make sure we get a *local* collection,
    # not a library-linked collection.
    collection = None
    for col in bpy.data.collections:
        if col.name == COLLECTION_NAME and col.library == None:
            collection = col
            break
    if collection == None:
        collection = bpy.data.collections.new(COLLECTION_NAME)
        collection.hide_viewport = True
        collection.hide_render = True
        collection.hide_select = True

    # Link the collection and get it appropriately set up.
    context.scene.collection.children.link(collection)
    for layer in context.scene.view_layers:
        if collection.name in layer.layer_collection.children:
            layer.layer_collection.children[collection.name].exclude = True

    return collection


# The main function that actually does the real work of this addon.
# It's called whenever anything relevant in the shake list on a
# camera is changed, and just tears down and completely rebuilds
# the camera-shake setup for it.
def rebuild_camera_shakes(camera, context):
    if camera.library != None:
        # Skip library-linked cameras.
        return

    collection = ensure_camera_shakify_collection(context)

    #----------------
    # First, completely tear down the current setup, if any.
    #----------------

    # Remove shake constraints from the camera.
    remove_list = []
    for constraint in camera.constraints:
        if starts_with_any_base_name(constraint.name):
            constraint.driver_remove("influence")
            remove_list += [constraint]
    for constraint in remove_list:
        camera.constraints.remove(constraint)

    # Remove shake empties for this camera.
    name_match = re.compile("{}_[0-9]+".format(re.escape(BASE_NAME + "_" + camera.name)))
    for obj in collection.objects:
        if name_match.fullmatch(obj.name) != None:
            obj.constraints[0].driver_remove("eval_time")
            obj.animation_data_clear()
            bpy.data.objects.remove(obj)

    #----------------
    # Then build the new setup.
    #----------------

    for shake_item_index in range(0, len(camera.camera_shakes)):
        build_single_shake(camera, shake_item_index, collection, context)

    #----------------
    # Finally, clean up any data that's no longer needed, up to and
    # including removing the collection itself if there no shakes left.
    #----------------

    # If there's nothing left in the collection, delete it.
    if len(collection.objects) == 0:
        context.scene.collection.children.unlink(collection)
        if collection.users == 0:
            bpy.data.collections.remove(collection)


# Fixes camera shake setups across the whole scene.
# This can be necessary if e.g. a user has duplicated cameras
# around, etc.
def fix_camera_shakes_globally(context):
    # Delete the collection and everything in it.
    collection = ensure_camera_shakify_collection(context)
    for obj in collection.objects:
        obj.constraints[0].driver_remove("eval_time")
        obj.animation_data_clear()
        bpy.data.objects.remove(obj)
    context.scene.collection.children.unlink(collection)
    if collection.users == 0:
        bpy.data.collections.remove(collection)

    # Remove shake channelbags in the shake action, to force them to get
    # re-built.
    action = ensure_action(ACTION_NAME)
    for channelbag in action.layers[0].strips[0].channelbags:
        action.layers[0].strips[0].channelbags.remove(channelbag)

    # Loop through all cameras and re-build their camera shakes.
    for obj in context.scene.objects:
        if obj.type == 'CAMERA':
            rebuild_camera_shakes(obj, context)


def on_shake_type_update(shake_instance, context):
    rebuild_camera_shakes(shake_instance.id_data, context)


def _shake_type_items(self, context):
    presets = get_all_presets()
    return [(id, get_display_name(id), "") for id in presets.keys()]


#class ActionToPythonData(bpy.types.Operator):
#    """Writes the action on the currently selected object to a text block as Python data"""
#    bl_idname = "object.action_to_python_data"
#    bl_label = "Action to Python Data"
#    bl_options = {'UNDO'}
#
#    @classmethod
#    def poll(cls, context):
#        return context.active_object is not None \
#               and context.active_object.animation_data is not None \
#               and context.active_object.animation_data.action is not None
#
#    def execute(self, context):
#        action_to_python_data_text(context.active_object.animation_data.action, "action_output.txt")
#        return {'FINISHED'}


class CameraShakeAdd(bpy.types.Operator):
    """Adds the selected camera shake to the list"""
    bl_idname = "object.camera_shake_add"
    bl_label = "Add Shake Item"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'CAMERA'

    def execute(self, context):
        camera = context.active_object
        shake = camera.camera_shakes.add()
        camera.camera_shakes_active_index = len(camera.camera_shakes) - 1
        rebuild_camera_shakes(camera, context)
        return {'FINISHED'}


class CameraShakeRemove(bpy.types.Operator):
    """Removes the selected camera shake item from the list"""
    bl_idname = "object.camera_shake_remove"
    bl_label = "Remove Shake Item"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'CAMERA' and len(obj.camera_shakes) > 0

    def execute(self, context):
        camera = context.active_object
        if camera.camera_shakes_active_index < len(camera.camera_shakes):
            camera.camera_shakes.remove(camera.camera_shakes_active_index)
            rebuild_camera_shakes(camera, context)
            if camera.camera_shakes_active_index >= len(camera.camera_shakes) and camera.camera_shakes_active_index > 0:
                camera.camera_shakes_active_index -= 1
        return {'FINISHED'}


class CameraShakeMove(bpy.types.Operator):
    """Moves the selected camera shake up/down in the list"""
    bl_idname = "object.camera_shake_move"
    bl_label = "Move Shake Item"
    bl_options = {'UNDO'}

    type: bpy.props.EnumProperty(items = [
        ('UP', "", ""),
        ('DOWN', "", ""),
    ])

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'CAMERA' and len(obj.camera_shakes) > 1

    def execute(self, context):
        camera = context.active_object
        index = int(camera.camera_shakes_active_index)
        if self.type == 'UP' and index > 0:
            camera.camera_shakes.move(index, index - 1)
            camera.camera_shakes_active_index -= 1
        elif self.type == 'DOWN' and (index + 1) < len(camera.camera_shakes):
            camera.camera_shakes.move(index, index + 1)
            camera.camera_shakes_active_index += 1
        rebuild_camera_shakes(camera, context)
        return {'FINISHED'}


class CameraShakesFixGlobal(bpy.types.Operator):
    """Ensures that all camera shakes in the scene are set up properly. This generally shouldn't be necessary, but if things are behaving strangely this should fix it"""
    bl_idname = "object.camera_shakes_fix_global"
    bl_label = "Fix All Camera Shakes"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        fix_camera_shakes_globally(context)
        return {'FINISHED'}


class CameraShakifyPrepFileForFarm(bpy.types.Operator):
    """Adds an auto-execute script to the blend file that makes Camera Shakes work even when the addon is not present. Particularly useful for sending files to a render farm. This only needs to be run once per file, not every time you submit a file to a farm"""
    bl_idname = "wm.camera_shakify_prep_file_for_farm"
    bl_label = "Prep Blend File For Render Farm"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        ensure_farm_script(INFLUENCE_MAX, SCALE_MAX)
        return {'FINISHED'}


class CameraShakifyImportCOLMAP(bpy.types.Operator):
    """Import COLMAP reconstruction as a new shake preset"""
    bl_idname = "wm.camera_shakify_import_colmap"
    bl_label = "Import COLMAP Shake"
    bl_options = {'UNDO'}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        import math

        path = self.filepath
        if not path:
            self.report({'ERROR'}, "No file selected")
            return {'CANCELLED'}

        # Check if it's a directory (scene folder) or images.txt
        up_levels = 2
        if os.path.isdir(path):
            for cand in ["sparse/images.txt", "sparse/0/images.txt"]:
                fp = os.path.join(path, cand)
                if os.path.exists(fp):
                    path = fp
                    up_levels = cand.count(os.sep) + 1
                    break
            else:
                self.report({'ERROR'}, f"No images.txt found in {path}/sparse/")
                return {'CANCELLED'}

        if not path.endswith("images.txt") or not os.path.exists(path):
            self.report({'ERROR'}, f"File not found: {path}")
            return {'CANCELLED'}

        # Parse
        def parse_images_txt(fp):
            poses = []
            with open(fp, 'r') as f:
                lines = f.readlines()
            i = 0
            while i < len(lines) and lines[i].startswith('#'):
                i += 1
            while i < len(lines):
                line = lines[i].strip()
                if not line or line.startswith('#'):
                    i += 1
                    continue
                parts = line.split()
                qw, qx, qy, qz = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
                tx, ty, tz = float(parts[5]), float(parts[6]), float(parts[7])
                poses.append((qw, qx, qy, qz, tx, ty, tz))
                i += 2
            return poses

        def quat_to_euler(qw, qx, qy, qz):
            sr = 2.0 * (qw * qx + qy * qz)
            cr = 1.0 - 2.0 * (qx * qx + qy * qy)
            ex = math.atan2(sr, cr)
            sp = 2.0 * (qw * qy - qz * qx)
            ey = math.copysign(math.pi / 2, sp) if abs(sp) >= 1 else math.asin(sp)
            sy = 2.0 * (qw * qz + qx * qy)
            cy = 1.0 - 2.0 * (qy * qy + qz * qz)
            ez = math.atan2(sy, cy)
            return ex, ey, ez

        def gauss_kernel(size, sigma):
            k = [math.exp(-0.5 * (x / sigma) ** 2) for x in range(-size, size + 1)]
            t = sum(k)
            return [x / t for x in k]

        def apply_kernel(data, kernel):
            n, k, half = len(data), len(kernel), len(kernel) // 2
            result = [0.0] * n
            for i in range(n):
                total = 0.0
                for j in range(k):
                    idx = i + j - half
                    if idx < 0:
                        idx = 0
                    elif idx >= n:
                        idx = n - 1
                    total += data[idx] * kernel[j]
                result[i] = total
            return result

        def lowpass(data, sigma):
            ks = max(1, int(sigma * 3))
            return apply_kernel(data, gauss_kernel(ks, sigma))

        def bandpass(data, sl, sh):
            a = lowpass(data, sl)
            b = lowpass(data, sh)
            return [a[i] - b[i] for i in range(len(data))]

        def loop_fix(data):
            """Force first == last by distributing a linear correction across all frames."""
            n2 = len(data)
            d = data[0] - data[-1]
            return [data[i] - d + d * i / (n2 - 1) for i in range(n2)]

        poses = parse_images_txt(path)
        n = len(poses)

        if n < 10:
            self.report({'ERROR'}, f"Only {n} frames — need at least 10")
            return {'CANCELLED'}

        loc = [[p[4] for p in poses], [p[5] for p in poses], [p[6] for p in poses]]
        eulers = [quat_to_euler(p[0], p[1], p[2], p[3]) for p in poses]
        rot = [[e[0] for e in eulers], [e[1] for e in eulers], [e[2] for e in eulers]]

        # Unwrap eulers
        for axis_idx in range(3):
            uw = [rot[axis_idx][0]]
            for i in range(1, n):
                d = rot[axis_idx][i] - uw[-1]
                uw.append(rot[axis_idx][i] - round(d / (2 * math.pi)) * 2 * math.pi)
            rot[axis_idx] = uw

        # Processed version: bandpass + loop_fix, scaled to INVESTIGATION level
        sl = max(5.0, n / 12.0)
        sh = max(1.5, n / 80.0)

        channels = {}
        for ax in range(3):
            bp = bandpass(loc[ax], sl, sh)
            channels[('location', ax)] = [v * 1.8 for v in bp]
        for ax in range(3):
            bp = bandpass(rot[ax], sl, sh)
            channels[('rotation_euler', ax)] = [v * 1.8 for v in bp]

        # Scale location/rotation max to INVESTIGATION level
        inv_data = SHAKE_LIST["INVESTIGATION"][2]
        inv_loc_max = 0.0
        inv_rot_max = 0.0
        for ck, vals in inv_data.items():
            m = max(abs(v[1]) for v in vals)
            if ck[0] == 'location':
                inv_loc_max = max(inv_loc_max, m)
            else:
                inv_rot_max = max(inv_rot_max, m)

        for ck_type, inv_max in [('location', inv_loc_max), ('rotation_euler', inv_rot_max)]:
            our_max = 0.0
            for ax in range(3):
                our_max = max(our_max, max(abs(v) for v in channels[(ck_type, ax)]))
            if our_max > 0:
                s = inv_max / our_max
                for ax in range(3):
                    channels[(ck_type, ax)] = [v * s for v in channels[(ck_type, ax)]]

        for ck in list(channels.keys()):
            channels[ck] = loop_fix(channels[ck])

        # Generate key/name from folder
        scene_path = path
        for _ in range(up_levels):
            scene_path = os.path.dirname(scene_path)
        folder = os.path.basename(scene_path)
        base_name = folder.replace("_", " ").title()
        import os as _os
        username = _os.environ.get('USERNAME', '')
        user_tag = f" ({username})" if username else ""
        name = base_name + user_tag
        key = re.sub(r'[^a-zA-Z0-9_]', '_', folder).strip('_').upper()
        while key in get_all_presets():
            key += "_2"

        # Format for storage
        fmt_channels = {}
        for ck in channels:
            fmt_channels[ck] = [(i, round(channels[ck][i], 6)) for i in range(n)]

        add_user_preset(key, name, 30.0, fmt_channels)

        self.report({'INFO'}, f"Imported '{name}' ({n} frames) as '{key}'")
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class CameraShakifyDeletePreset(bpy.types.Operator):
    """Delete a user-imported shake preset (built-in presets are locked)"""
    bl_idname = "wm.camera_shakify_delete_preset"
    bl_label = "Delete Preset"
    bl_options = {'UNDO'}

    preset_key: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        if is_builtin(self.preset_key):
            self.report({'ERROR'}, f"Cannot delete built-in preset '{self.preset_key}'")
            return {'CANCELLED'}
        remove_user_preset(self.preset_key)
        self.report({'INFO'}, f"Deleted preset '{self.preset_key}'")
        return {'FINISHED'}


# An actual instance of Camera shake added to a camera.
#
# IMPORTANT: when making changes here (properties), make sure to also update
# the corresponding class in farm_script.py.
class CameraShakeInstance(bpy.types.PropertyGroup):
    shake_type: bpy.props.EnumProperty(
        name = "Shake Type",
        items = _shake_type_items,
        options = set(), # Not animatable.
        override = set(), # Not library overridable.
        update = on_shake_type_update,
    )
    influence: bpy.props.FloatProperty(
        name="Influence",
        description="How much the camera shake affects the camera",
        default=1.0,
        min=0.0, max=INFLUENCE_MAX,
        soft_min=0.0, soft_max=1.0,
    )
    scale: bpy.props.FloatProperty(
        name="Scale",
        description="The scale of the shake's location component",
        default=1.0,
        min=0.0, max=SCALE_MAX,
        soft_min=0.0, soft_max=2.0,
    )
    use_location: bpy.props.BoolProperty(
        name="Use Location",
        description="Enable location (translation) shake",
        default=True,
    )
    use_rotation: bpy.props.BoolProperty(
        name="Use Rotation",
        description="Enable rotation shake",
        default=True,
    )
    use_manual_timing: bpy.props.BoolProperty(
        name="Manual Timing",
        description="Manually animate the progression of time through the camera shake animation",
        default=False,
    )
    time: bpy.props.FloatProperty(
        name="Time",
        description="Current time (in frame number) of the shake animation",
        default=0.0,
        precision=1,
        step=100.0,
    )
    speed: bpy.props.FloatProperty(
        name="Speed",
        description="Multiplier for how fast the shake animation plays",
        default=1.0,
        soft_min=0.0, soft_max=4.0,
        options = set(), # Not animatable.
    )
    offset: bpy.props.FloatProperty(
        name="Frame Offset",
        description="How many frames to offset the shake animation",
        default=0.0,
        precision=1,
        step=100.0,
    )
    use_loop_range: bpy.props.BoolProperty(
        name="Loop Range",
        description="Resample the shake to the scene frame range and loop seamlessly (speed still applies, offset ignored)",
        default=False,
        update=lambda self, ctx: rebuild_camera_shakes(self.id_data, ctx),
    )



#========================================================


class CameraShakifyRenamePreset(bpy.types.Operator):
    """Rename a shake preset"""
    bl_idname = "wm.camera_shakify_rename_preset"
    bl_label = "Rename Preset"
    bl_options = {'UNDO'}

    preset_key: bpy.props.StringProperty(options={'HIDDEN'})
    new_name: bpy.props.StringProperty(name="New Name")

    def invoke(self, context, event):
        self.new_name = get_display_name(self.preset_key)
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        new_name = self.new_name.strip()
        if not new_name:
            self.report({'ERROR'}, "Name cannot be empty")
            return {'CANCELLED'}
        set_display_name(self.preset_key, new_name)
        return {'FINISHED'}


class CameraShakifyResetNames(bpy.types.Operator):
    """Reset all built-in preset names to their defaults"""
    bl_idname = "wm.camera_shakify_reset_names"
    bl_label = "Reset Default Names"
    bl_options = {'UNDO'}

    def execute(self, context):
        reset_display_names()
        self.report({'INFO'}, "Preset names reset to defaults")
        return {'FINISHED'}


class CameraShakifyPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    def draw(self, context):
        layout = self.layout
        presets = get_all_presets()
        user_presets = load_user_presets()

        layout.label(text="Installed Shake Presets:")
        box = layout.box()
        for key in presets:
            display_name = get_display_name(key)
            row = box.row()
            row.label(text=display_name)
            row.operator("wm.camera_shakify_rename_preset", text="", icon='GREASEPENCIL').preset_key = key
            if not is_builtin(key):
                op = row.operator("wm.camera_shakify_delete_preset", text="", icon='X')
                op.preset_key = key

        if not user_presets:
            row = box.row()
            row.label(text="No user-imported presets yet. Use Import COLMAP in the camera Properties panel.", icon='INFO')

        layout.separator()
        layout.operator("wm.camera_shakify_reset_names", icon='LOOP_BACK')


#========================================================


def register():
    bpy.utils.register_class(CameraShakifyPanel)
    bpy.utils.register_class(OBJECT_UL_camera_shake_items)
    bpy.utils.register_class(CameraShakeInstance)
    bpy.utils.register_class(CameraShakeAdd)
    bpy.utils.register_class(CameraShakeRemove)
    bpy.utils.register_class(CameraShakeMove)
    bpy.utils.register_class(CameraShakesFixGlobal)
    bpy.utils.register_class(CameraShakifyPrepFileForFarm)
    bpy.utils.register_class(CameraShakifyImportCOLMAP)
    bpy.utils.register_class(CameraShakifyDeletePreset)
    bpy.utils.register_class(CameraShakifyRenamePreset)
    bpy.utils.register_class(CameraShakifyResetNames)
    bpy.utils.register_class(CameraShakifyPreferences)

    # # Only needed for creating new shakes to add to this addon. Not for end users.
    # bpy.utils.register_class(ActionToPythonData)
    # bpy.types.VIEW3D_MT_object.append(
    #     lambda self, context : self.layout.operator(ActionToPythonData.bl_idname)
    # )

    # The list of camera shakes active on an camera, along with each shake's parameters.
    bpy.types.Object.camera_shakes = bpy.props.CollectionProperty(type=CameraShakeInstance)
    bpy.types.Object.camera_shakes_active_index = bpy.props.IntProperty(name="Camera Shake List Active Item Index", options = set())

    bpy.types.WindowManager.camera_shake_show_utils = bpy.props.BoolProperty(name="Show Camera Shake Utils UI", default=False)


def unregister():
    del bpy.types.Object.camera_shakes
    del bpy.types.Object.camera_shakes_active_index

    bpy.utils.unregister_class(CameraShakifyPanel)
    bpy.utils.unregister_class(OBJECT_UL_camera_shake_items)
    bpy.utils.unregister_class(CameraShakeInstance)
    bpy.utils.unregister_class(CameraShakeAdd)
    bpy.utils.unregister_class(CameraShakeRemove)
    bpy.utils.unregister_class(CameraShakeMove)
    bpy.utils.unregister_class(CameraShakesFixGlobal)
    bpy.utils.unregister_class(CameraShakifyPrepFileForFarm)
    bpy.utils.unregister_class(CameraShakifyImportCOLMAP)
    bpy.utils.unregister_class(CameraShakifyDeletePreset)
    bpy.utils.unregister_class(CameraShakifyResetNames)
    bpy.utils.unregister_class(CameraShakifyRenamePreset)
    bpy.utils.unregister_class(CameraShakifyPreferences)

    del bpy.types.WindowManager.camera_shake_show_utils

    #bpy.utils.unregister_class(ActionToPythonData)


if __name__ == "__main__":
    register()
