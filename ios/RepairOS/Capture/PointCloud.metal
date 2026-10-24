// Live LiDAR point cloud over the camera image.
// The depth unprojection and the YCbCr-to-RGB conversion are adapted from Apple's sample "Displaying a point cloud
// using scene depth" (shaders.metal, pointCloudVertexShader; licence in ThirdParty/AppleScenePointCloud/LICENSE.txt).
// Changed: points are unprojected into ARKit world space and kept one per voxel in a GPU hash (accumulate), the
// camera image is drawn behind them, and points outside the locked box are dimmed.

#include <metal_stdlib>
using namespace metal;

struct Particle {
    float4 positionConf;   // xyz world metres, w = ARKit confidence (0/1/2); w < 0 = empty slot
    float4 color;          // rgb 0...1
};

struct BoxUniforms {
    float4 center;         // xyz, w = 1 when a box is locked
    float4 halfSize;       // xyz half extents, w = yaw (radians)
};

static half3 ycbcrToRGB(half y, half2 cbcr) {
    half2 uv = cbcr - half2(0.5h, 0.5h);
    return half3(y + 1.402h * uv.y, y - 0.7141h * uv.y - 0.3441h * uv.x, y + 1.772h * uv.x);
}

static bool insideBox(float3 p, constant BoxUniforms &box, float margin) {
    float3 d = p - box.center.xyz;
    float c = cos(-box.halfSize.w), s = sin(-box.halfSize.w);
    float3 local = float3(c * d.x + s * d.z, d.y, -s * d.x + c * d.z);
    return all(abs(local) <= box.halfSize.xyz + margin);
}

struct GridUniforms {
    float4 fine;           // xyz origin, w = cell size (m); inside the locked box
    float4 coarse;         // xyz origin, w = cell size (m); everywhere else
};

constant uint EMPTY_KEY = 0xFFFFFFFFu;
constant uint CELLS = 1024u;           // per axis, per level (10 bits)
constant uint MAX_PROBES = 32u;

static uint cellKey(float3 w, float4 grid, uint level, thread bool &ok) {
    float3 c = floor((w - grid.xyz) / grid.w);
    ok = all(c >= 0.0f) && all(c < float(CELLS));
    uint3 u = uint3(c);
    return (level << 30) | (u.x << 20) | (u.y << 10) | u.z;
}

// One thread per sampled depth pixel: unproject into world space and keep ONE point per voxel (2.5 mm inside the
// locked box, 1 cm elsewhere) in a GPU hash. A new observation replaces the stored point only when it is better:
// higher confidence, or the same confidence seen at higher resolution (cos(angle to the surface) / distance, the
// normal from neighbouring depth samples). Voxels never age out, so a side scanned first is never overwritten.
kernel void accumulatePoints(texture2d<float, access::read> depthTexture [[ texture(0) ]],
                             texture2d<float, access::read> confTexture [[ texture(1) ]],
                             texture2d<half> colorY [[ texture(2) ]],
                             texture2d<half> colorCbCr [[ texture(3) ]],
                             constant float4x4 &cameraToWorld [[ buffer(0) ]],
                             constant float3x3 &intrinsics [[ buffer(1) ]],     // at depth resolution
                             constant uint4 &params [[ buffer(2) ]],            // step, capacity (power of 2), minConf, maxDepthMM
                             constant BoxUniforms &box [[ buffer(3) ]],
                             device Particle *particles [[ buffer(4) ]],
                             device atomic_uint *counter [[ buffer(5) ]],
                             device atomic_uint *keys [[ buffer(6) ]],
                             device atomic_uint *scores [[ buffer(7) ]],
                             constant GridUniforms &grid [[ buffer(8) ]],
                             uint2 gid [[ thread_position_in_grid ]])
{
    uint step = params.x;
    uint2 pos = gid * step;
    uint W = depthTexture.get_width(), H = depthTexture.get_height();
    if (pos.x >= W || pos.y >= H) return;
    float depth = depthTexture.read(pos).x;
    int conf = int(round(255.0f * confTexture.read(pos).r));
    if (conf < int(params.z) || depth <= 0.05f || depth * 1000.0f > float(params.w)) return;

    // Image axes (x right, y down, z forward) to ARKit camera axes (x right, y up, -z forward).
    float fx = intrinsics[0][0], fy = intrinsics[1][1], cx = intrinsics[2][0], cy = intrinsics[2][1];
    float3 pc = float3((float(pos.x) - cx) * depth / fx, -(float(pos.y) - cy) * depth / fy, -depth);
    float4 world = cameraToWorld * float4(pc, 1.0f);

    // Resolution of this observation: cos(view angle to the surface) / distance. The normal comes from the
    // neighbouring depth samples; without them assume 60°.
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

    bool ok;
    bool fine = box.center.w > 0.5f && insideBox(world.xyz, box, 0.01f);
    uint key = fine ? cellKey(world.xyz, grid.fine, 1u, ok) : cellKey(world.xyz, grid.coarse, 0u, ok);
    if (!ok) return;

    uint cap = params.y;
    uint slot = (key * 2654435761u) & (cap - 1u);
    for (uint probe = 0; probe < MAX_PROBES; probe++) {
        uint expected = EMPTY_KEY;
        if (atomic_compare_exchange_weak_explicit(&keys[slot], &expected, key, memory_order_relaxed, memory_order_relaxed)) {
            atomic_fetch_add_explicit(counter, 1u, memory_order_relaxed);
            expected = key;
        }
        if (expected == key) {
            uint old = atomic_fetch_max_explicit(&scores[slot], score, memory_order_relaxed);
            if (score > old) {
                constexpr sampler s(mag_filter::linear, min_filter::linear);
                float2 uv = float2(pos) / float2(W - 1, H - 1);
                half3 rgb = clamp(ycbcrToRGB(colorY.sample(s, uv).r, colorCbCr.sample(s, uv).rg), 0.0h, 1.0h);
                particles[slot].positionConf = float4(world.xyz, float(conf));
                particles[slot].color = float4(float3(rgb), 1.0f);
            }
            return;
        }
        slot = (slot + 1u) & (cap - 1u);
    }
}

struct ParticleOut {
    float4 position [[position]];
    float pointSize [[point_size]];
    half4 color;
};

vertex ParticleOut particleVertex(uint vid [[ vertex_id ]],
                                  const device Particle *particles [[ buffer(0) ]],
                                  constant float4x4 &viewProjection [[ buffer(1) ]],
                                  constant BoxUniforms &box [[ buffer(2) ]],
                                  constant float &pointScale [[ buffer(3) ]])
{
    ParticleOut out;
    Particle p = particles[vid];
    if (p.positionConf.w < 0.0f) {
        out.position = float4(2, 2, 2, 1);   // empty slot: outside the clip volume
        out.pointSize = 0;
        out.color = half4(0);
        return out;
    }
    float4 clip = viewProjection * float4(p.positionConf.xyz, 1.0f);
    out.position = clip;
    bool dim = box.center.w > 0.5f && !insideBox(p.positionConf.xyz, box, 0.01f);
    out.pointSize = clamp(pointScale / max(clip.w, 0.05f), 1.5f, 12.0f) * (dim ? 0.6f : 1.0f);
    out.color = half4(half3(p.color.rgb), dim ? 0.25h : 1.0h);
    return out;
}

fragment half4 particleFragment(ParticleOut in [[ stage_in ]], float2 pc [[ point_coord ]])
{
    // Round points.
    if (length(pc - float2(0.5)) > 0.5) discard_fragment();
    return in.color;
}

struct CameraOut {
    float4 position [[position]];
    float2 uv;
};

vertex CameraOut cameraVertex(uint vid [[ vertex_id ]], constant float4 *quad [[ buffer(0) ]])
{
    CameraOut out;
    out.position = float4(quad[vid].xy, 0.0f, 1.0f);
    out.uv = quad[vid].zw;
    return out;
}

fragment half4 cameraFragment(CameraOut in [[ stage_in ]],
                              texture2d<half> colorY [[ texture(0) ]],
                              texture2d<half> colorCbCr [[ texture(1) ]])
{
    constexpr sampler s(mag_filter::linear, min_filter::linear);
    return half4(clamp(ycbcrToRGB(colorY.sample(s, in.uv).r, colorCbCr.sample(s, in.uv).rg), 0.0h, 1.0h), 1.0h);
}
