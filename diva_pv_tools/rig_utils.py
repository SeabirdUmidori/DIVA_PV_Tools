r"""Move the pelvis rotation off the spine, and give the body facing a carrier that exists.

Three changes, applied in memory at export time so an unmodified template still exports correctly:

    kl_hara_etc   COPY_ROTATION 下半身 -> 腰              (LOCAL / LOCAL_OWNER_ORIENT)
    kl_kosi_xz    (no driver)        -> COPY_ROTATION 下半身 (LOCAL / LOCAL_OWNER_ORIENT)
    kl_hara_xz    COPY_ROTATION センター -> グルーブ          (WORLD / WORLD)

Why the first two.  The template's Import Rig lays the MMD skeleton out in the DIVA shape with
unanimated spacers:

    センター -> グルーブ -> 腰 -> kl_hara_etc -> n_hara -> kl_kosi_y -> kl_kosi_xz -> 下半身
                                     \-> cl_mune -> 上半身 -> n_mune_b -> 上半身2 -> 首 -> 頭

so the chest branch leaves the spine at `kl_hara_etc`, and `下半身` sits on the *leg* side, below
`kl_kosi_xz`.  The shipped Export Rig instead copies `下半身` into `kl_hara_etc`, i.e. into the spine
above the point where the chest branches off - so the chest inherits a pelvis rotation the source
never routes to it, while `kl_kosi_xz`, the bone actually above the hips, gets nothing at all.

Measured on the reference dance: the hip line already matched perfectly and the error was entirely in the twist
between pelvis and chest.  At frame 1410 the shipped mapping put the shoulder line 73.4° off the
source's torso twist, the fixed mapping 0.0° off.

And the twist error is 0.0 at every sampled frame of four windows (0..92, 11006..11072, 11693..11804,
1390..1440), with the turns themselves unchanged - the 0..92 spin still ends at -171.8°.

Why the third.  Project DIVA carries the whole-body facing on the spine root, and `kl_hara_xz` is the
bone our written file actually puts it on (shipped DIVA files show `kg_hara_y` written as nothing at
all while `kl_hara_xz` carries 178.7° on the reference dance).  But MMD rigs put that facing in `グルーブ` just as
often as in `センター`, and the old copy only watched `センター`.  Measured on DIVA-extracted
motions: the worst body turn - max 178°, cumulative 8324° - sits
entirely in `グルーブ`, `センター` and `下半身` stay at 0, and no Export Rig bone copied `グルーブ`, so
the turn was dropped on the floor.

Why the WORLD / WORLD form is safe.  Retargeting `kl_hara_xz` onto `グルーブ` with this function's
LOCAL / LOCAL_OWNER_ORIENT spaces would copy the bone's *own* rotation - which for a bone the dance
never animates is zero, so every spin vanishes (the 0..92 turn would fall from -181° to -11°).  A
WORLD / WORLD copy instead takes `グルーブ`'s world rotation, and `グルーブ` rests on exactly the same
axes as `センター` (measured rest offset 0.00° in the Import Rig), so for any song with an idle
`グルーブ` the copied value is identical to the old `センター` copy - a strict superset, checked by
re-exporting the reference dance and comparing the mot set byte for byte with the accepted one.

What still is NOT fixed here: a mocap source that puts the facing in `下半身` and `上半身` (three shipped
mocap-sourced songs whose `kl_kosi_xz`/`cl_mune` land at relMax≈180° *and*
sideMax≈180°) still gets that half turn on the bones below the chest fork, because the total rotation
is conserved along the chain: hoisting it above the fork without rotating the leg targets moves the
legs (measured at 73.5 cm and 191 cm), and pinning the visible bones with WORLD
copies instead pushes the same half turn into the head (cl_kao 44.1° -> 178.2°).
"""
import bpy

# Export Rig bone -> (source bone, owner_space, target_space).
SPINE_COPIES = (
    ("kl_hara_etc", "腰", 'LOCAL', 'LOCAL_OWNER_ORIENT'),
    ("kl_kosi_xz", "下半身", 'LOCAL', 'LOCAL_OWNER_ORIENT'),
    ("kl_hara_xz", "グルーブ", 'WORLD', 'WORLD'),
)
SOURCE_ARMATURE = "Import Rig"


def apply_spine_copies(armature, source_name=SOURCE_ARMATURE):
    """Route the pelvis rotation through the hip bone and the facing through the spine root."""
    source = bpy.data.objects.get(source_name)
    if source is None:
        return ["source armature %s not found, spine left as the file has it" % source_name]
    changed = []
    for bone_name, subtarget, owner_space, target_space in SPINE_COPIES:
        pose_bone = armature.pose.bones.get(bone_name)
        if pose_bone is None:
            changed.append("no %s bone on %s" % (bone_name, armature.name))
            continue
        if subtarget not in source.pose.bones:
            changed.append("no %s bone on %s" % (subtarget, source_name))
            continue
        copy = next((c for c in pose_bone.constraints if c.type == 'COPY_ROTATION' and not c.mute), None)
        if copy is None:
            copy = pose_bone.constraints.new('COPY_ROTATION')
            copy.name = "DIVA spine mapping"
            changed.append("%s: no driver -> copy %s" % (bone_name, subtarget))
        elif copy.subtarget != subtarget:
            changed.append("%s: copy %s -> %s" % (bone_name, copy.subtarget, subtarget))
        copy.target = source
        copy.subtarget = subtarget
        # LOCAL/LOCAL_OWNER_ORIENT keeps a copy incremental: the bone's own rotation relative to its
        # parent, which is what a mot channel stores.  WORLD/WORLD is what a facing carrier needs,
        # because the value must survive whatever the bone above it is already doing.
        copy.owner_space = owner_space
        copy.target_space = target_space
    return changed
