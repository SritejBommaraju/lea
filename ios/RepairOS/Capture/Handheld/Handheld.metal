// The hand-held ("Turn it") cloud: LiDAR samples fused in the OBJECT frame (the tracker's pose per frame) into the
// same GPU voxel hash as PointCloud.metal, and drawn where the object is now. Helpers repeated from PointCloud.metal
// (each .metal file compiles on its own); the colour conversion there is adapted from Apple's sample (see that file).

#include <metal_stdlib>
using namespace metal;

struct HHParticle {
    float4 positionConf;   // xyz (object frame inside the box, world outside), w = confidence; w < 0 = empty
    float4 color;
};

struct HHBox {
    float4 center;         // xyz, w = 1 when locked
    float4 halfSize;       // xyz half extents, w = yaw
};

struct HHGrid {
    float4 fine;
    float4 coarse;
};

struct HHFusion {
    float4x4 cameraToWorld;
    float4x4 cameraToObject;
    float4 intrinsics;     // fx, fy, cx, cy at depth resolution
    float4 plane;          // x = plane y (world), y = clearance (m), z = 1 when a plane is known
    uint4 params;          // step, capacity, minConf, maxDepthMM
};

constant uint HH_EMPTY = 0xFFFFFFFFu;
constant uint HH_CELLS = 1024u;
constant uint HH_PROBES = 32u;

static bool hhInsideBox(float3 p, constant HHBox &box, float margin) {
    float3 d = p - box.center.xyz;
    float c = cos(-box.halfSize.w), s = sin(-box.halfSize.w);
    float3 local = float3(c * d.x + s * d.z, d.y, -s * d.x + c * d.z);
    return all(abs(local) <= box.halfSize.xyz + margin);
}

// One thread per sampled depth pixel of a tracked frame: not the person, above the table (world), inside the box in
// the object frame; one point per 2.5 mm voxel, replaced only by a better observation (as accumulatePoints).
kernel void accumulateObjectPoints(texture2d<float, access::read> depthTexture [[ texture(0) ]],
                                   texture2d<float, access::read> confTexture [[ texture(1) ]],
                                   texture2d<float, access::read> colorTexture [[ texture(2) ]],
                                   texture2d<float, access::read> maskTexture [[ texture(3) ]],
                                   constant HHFusion &f [[ buffer(0) ]],
                                   constant HHBox &box [[ buffer(1) ]],
                                   device HHParticle *particles [[ buffer(2) ]],
                                   device atomic_uint *counter [[ buffer(3) ]],
                                   device atomic_uint *keys [[ buffer(4) ]],
                                   device atomic_uint *scores [[ buffer(5) ]],
                                   constant HHGrid &grid [[ buffer(6) ]],
                                   uint2 gid [[ thread_position_in_grid ]])
{
    uint step = f.params.x;
    uint2 pos = gid * step;
    uint W = depthTexture.get_width(), H = depthTexture.get_height();
    if (pos.x >= W || pos.y >= H) return;
    float depth = depthTexture.read(pos).x;
    int conf = int(round(255.0f * confTexture.read(pos).r));
    if (conf < int(f.params.z) || depth <= 0.08f || depth * 1000.0f > float(f.params.w)) return;
    // The person (hand), dilated by one pixel.
    for (int dy = -1; dy <= 1; dy++) {
        for (int dx = -1; dx <= 1; dx++) {
            uint2 q = uint2(clamp(int(pos.x) + dx, 0, int(W) - 1), clamp(int(pos.y) + dy, 0, int(H) - 1));
            if (maskTexture.read(q).r > 0.5f) return;
        }
    }
    float fx = f.intrinsics.x, fy = f.intrinsics.y, cx = f.intrinsics.z, cy = f.intrinsics.w;
    float3 pc = float3((float(pos.x) - cx) * depth / fx, -(float(pos.y) - cy) * depth / fy, -depth);
    float4 world = f.cameraToWorld * float4(pc, 1.0f);
    if (f.plane.z > 0.5f && world.y < f.plane.x + f.plane.y) return;      // the table
    float4 obj = f.cameraToObject * float4(pc, 1.0f);
    if (!hhInsideBox(obj.xyz, box, 0.004f)) return;

    float cosA = 0.5f;
    uint2 px = uint2(min(pos.x + step, W - 1), pos.y), py = uint2(pos.x, min(pos.y + step, H - 1));
    float dx = depthTexture.read(px).x, dy = depthTexture.read(py).x;
    if (dx > 0.05f && dy > 0.05f && abs(dx - depth) < 0.05f && abs(dy - depth) < 0.05f) {
        float3 ax = float3((float(px.x) - cx) * dx / fx, -(float(px.y) - cy) * dx / fy, -dx) - pc;
        float3 ay = float3((float(py.x) - cx) * dy / fx, -(float(py.y) - cy) * dy / fy, -dy) - pc;
        float3 n = cross(ax, ay);
        if (length(n) > 1e-9f) cosA = abs(dot(normalize(n), normalize(-pc)));
    }
    uint quality = min(0xFFFFFFu, uint(cosA / depth * 100000.0f));
    uint score = (uint(conf) << 24) | quality;

    float3 c = floor((obj.xyz - grid.fine.xyz) / grid.fine.w);
    if (any(c < 0.0f) || any(c >= float(HH_CELLS))) return;
    uint3 u = uint3(c);
    uint key = (1u << 30) | (u.x << 20) | (u.y << 10) | u.z;
    uint cap = f.params.y;
    uint slot = (key * 2654435761u) & (cap - 1u);
    for (uint probe = 0; probe < HH_PROBES; probe++) {
        uint expected = HH_EMPTY;
        if (atomic_compare_exchange_weak_explicit(&keys[slot], &expected, key, memory_order_relaxed, memory_order_relaxed)) {
            atomic_fetch_add_explicit(counter, 1u, memory_order_relaxed);
            expected = key;
        }
        if (expected == key) {
            uint old = atomic_fetch_max_explicit(&scores[slot], score, memory_order_relaxed);
            if (score > old) {
                particles[slot].positionConf = float4(obj.xyz, float(conf));
                particles[slot].color = float4(colorTexture.read(pos).rgb, 1.0f);
            }
            return;
        }
        slot = (slot + 1u) & (cap - 1u);
    }
}

struct HHParticleOut {
    float4 position [[position]];
    float pointSize [[point_size]];
    half4 color;
};

struct HHFinish { float4 params; };

// As particleVertex, with the points inside the box (object frame) moved to where the object is now.
vertex HHParticleOut particleVertexObject(uint vid [[ vertex_id ]],
                                          const device HHParticle *particles [[ buffer(0) ]],
                                          constant float4x4 &viewProjection [[ buffer(1) ]],
                                          constant HHBox &box [[ buffer(2) ]],
                                          constant float &pointScale [[ buffer(3) ]],
                                          constant HHFinish &finish [[ buffer(4) ]],
                                          constant float4x4 &objectToWorld [[ buffer(5) ]])
{
    HHParticleOut out;
    HHParticle p = particles[vid];
    if (p.positionConf.w < 0.0f) {
        out.position = float4(2, 2, 2, 1);
        out.pointSize = 0;
        out.color = half4(0);
        return out;
    }
    float3 local = p.positionConf.xyz;
    bool locked = box.center.w > 0.5f;
    bool inside = locked && hhInsideBox(local, box, 0.01f);
    float3 world = inside ? (objectToWorld * float4(local, 1.0f)).xyz : local;
    float3 centre = (objectToWorld * float4(box.center.xyz, 1.0f)).xyz;
    bool dim = locked && !inside;
    float t = finish.params.x;
    float alpha = dim ? 0.18f : 1.0f;
    float grow = 1.0f;
    float3 rgb = p.color.rgb;
    if (t > 0.0f && locked) {
        if (dim) {
            float k = smoothstep(0.0f, 0.75f, t);
            world = mix(world, centre, k * k);
            alpha *= 1.0f - k;
        } else {
            float k = smoothstep(0.15f, 1.0f, t);
            world = mix(world, centre, 0.16f * k);
            rgb = mix(rgb, float3(0.61f, 0.58f, 1.0f), 0.45f * k);
            grow = 1.0f + 0.35f * k;
        }
    }
    float4 clip = viewProjection * float4(world, 1.0f);
    out.position = clip;
    out.pointSize = clamp(pointScale / max(clip.w, 0.05f), 1.5f, 12.0f) * (dim ? 0.6f : 1.0f) * grow;
    out.color = half4(half3(rgb), half(alpha));
    if (alpha <= 0.003f) { out.position = float4(2, 2, 2, 1); out.pointSize = 0; }
    return out;
}
