import ARKit
import Foundation
import simd

/// One LiDAR depth frame copied out of the ARFrame (the frame itself is never kept): depth in metres, ARKit
/// confidence, intrinsics scaled to the depth map, and the camera pose.
struct DepthCopy: Sendable {
    var width: Int, height: Int
    var depth: [Float]
    var confidence: [UInt8]
    var fx: Float, fy: Float, cx: Float, cy: Float
    var cameraToWorld: simd_float4x4
    var timestamp: TimeInterval

    static func from(_ frame: ARFrame) -> DepthCopy? {
        guard let d = frame.smoothedSceneDepth ?? frame.sceneDepth, let conf = d.confidenceMap else { return nil }
        let map = d.depthMap
        CVPixelBufferLockBaseAddress(map, .readOnly)
        CVPixelBufferLockBaseAddress(conf, .readOnly)
        defer {
            CVPixelBufferUnlockBaseAddress(map, .readOnly)
            CVPixelBufferUnlockBaseAddress(conf, .readOnly)
        }
        let w = CVPixelBufferGetWidth(map), h = CVPixelBufferGetHeight(map)
        guard let db = CVPixelBufferGetBaseAddress(map), let cb = CVPixelBufferGetBaseAddress(conf) else { return nil }
        let dRow = CVPixelBufferGetBytesPerRow(map), cRow = CVPixelBufferGetBytesPerRow(conf)
        var depth = [Float](repeating: 0, count: w * h), confidence = [UInt8](repeating: 0, count: w * h)
        for y in 0..<h {
            let dr = db.advanced(by: y * dRow).assumingMemoryBound(to: Float32.self)
            let cr = cb.advanced(by: y * cRow).assumingMemoryBound(to: UInt8.self)
            for x in 0..<w {
                depth[y * w + x] = dr[x]
                confidence[y * w + x] = cr[x]
            }
        }
        let k = frame.camera.intrinsics
        let iw = Float(CVPixelBufferGetWidth(frame.capturedImage)), ih = Float(CVPixelBufferGetHeight(frame.capturedImage))
        let sx = Float(w) / iw, sy = Float(h) / ih
        return DepthCopy(width: w, height: h, depth: depth, confidence: confidence,
                         fx: k[0][0] * sx, fy: k[1][1] * sy, cx: k[2][0] * sx, cy: k[2][1] * sy,
                         cameraToWorld: frame.camera.transform, timestamp: frame.timestamp)
    }

    /// World points with confidence ≥ `minConfidence` (ARKit: 0 low, 1 medium, 2 high), every `step` pixels.
    func worldPoints(minConfidence: UInt8 = 1, step: Int = 1, maxDepth: Float = 2) -> [SIMD3<Float>] {
        var out: [SIMD3<Float>] = []
        out.reserveCapacity(width * height / (step * step))
        for v in stride(from: 0, to: height, by: step) {
            for u in stride(from: 0, to: width, by: step) {
                let i = v * width + u
                let d = depth[i]
                guard confidence[i] >= minConfidence, d.isFinite, d > 0.05, d < maxDepth else { continue }
                // ARKit camera axes: +x right, +y up, -z forward; image v grows downward.
                let c = cameraToWorld * SIMD4((Float(u) - cx) * d / fx, -(Float(v) - cy) * d / fy, -d, 1)
                out.append(SIMD3(c.x, c.y, c.z))
            }
        }
        return out
    }

    /// Median depth in a small window around a point of the depth map (nil when there is no valid depth).
    func depth(atU u: Float, v: Float, radius: Int = 2) -> Float? {
        let cu = Int(u), cv = Int(v)
        guard cu >= 0, cv >= 0, cu < width, cv < height else { return nil }
        var vals: [Float] = []
        for y in max(0, cv - radius)...min(height - 1, cv + radius) {
            for x in max(0, cu - radius)...min(width - 1, cu + radius) {
                let d = depth[y * width + x]
                if d.isFinite, d > 0 { vals.append(d) }
            }
        }
        guard !vals.isEmpty else { return nil }
        return vals.sorted()[vals.count / 2]
    }
}

extension DepthCopy {
    /// The world point under a normalized sensor-image point (0…1, the depth map's own orientation): the nearest depth
    /// pixel, when ARKit's confidence is at least `minConfidence` and the depth is 5 cm to 5 m. Nil otherwise.
    func worldPoint(at s: CGPoint, minConfidence: UInt8 = 1) -> SIMD3<Float>? {
        let x = min(width - 1, max(0, Int(s.x * Double(width)))), y = min(height - 1, max(0, Int(s.y * Double(height))))
        let d = depth[y * width + x]
        guard confidence[y * width + x] >= minConfidence, d.isFinite, d > 0.05, d < 5 else { return nil }
        let ray = cameraRay(fx: fx, fy: fy, cx: cx, cy: cy, x: Float(s.x) * Float(width), y: Float(s.y) * Float(height))
        let p = cameraToWorld * SIMD4(ray * d, 1)
        return SIMD3(p.x, p.y, p.z)
    }
}

/// The camera-space ray through an image pixel (ARKit camera: x right, y up, looking down -z), with z = -1, from the
/// intrinsics of that image. Shared by tap-to-lock and Lea's pointing.
func cameraRay(fx: Float, fy: Float, cx: Float, cy: Float, x: Float, y: Float) -> SIMD3<Float> {
    SIMD3((x - cx) / fx, -(y - cy) / fy, -1)
}
