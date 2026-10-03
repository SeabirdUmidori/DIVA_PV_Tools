
import math

class KeySet:
    def __init__(self, keyset_type, values):
        self.type = keyset_type
        self.values = values

def continuous_euler(euler, prev):
    """The branch of an XYZ euler that continues `prev` instead of flipping away from it.

    to_euler('XYZ') has no memory of the previous frame.  Around the middle axis' +-pi/2
    boundary the same rotation also reads as (x+pi, pi-y, z+pi), and it returns whichever
    branch it feels like, so x and z jump by ~pi together while the bone barely turns.  Each
    channel can then be shifted by whole turns without changing the rotation at all, so the
    answer is the branch-and-whole-turn combination closest to what the last frame emitted -
    the game interpolates every channel on its own, and a step over pi makes that bone sweep
    half a revolution inside a single frame.
    """
    two_pi = math.pi * 2.0
    curr = (euler.x, euler.y, euler.z)
    if prev is None:
        return curr

    def nearest(base):
        return tuple(b + round((p - b) / two_pi) * two_pi for b, p in zip(base, prev))

    # Both families are always candidates: the branch flip can land just *under* pi, so a short
    # circuit that trusts a raw step under pi misses it and the engine injects a phantom turn.
    flipped = (curr[0] + math.pi, math.pi - curr[1], curr[2] + math.pi)
    return min((nearest(curr), nearest(flipped)),
               key=lambda c: max(abs(a - b) for a, b in zip(c, prev)))

def is_static(values, decimals):
    if not values:
        return True
    rounded_values = [round(value, decimals) for value in values]
    first_value = rounded_values[0]
    return all(value == first_value for value in rounded_values)
