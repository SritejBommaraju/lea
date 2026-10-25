import Foundation
import simd

/// Raw tracker frames on disk, for replaying a real hand-held scan on the Mac (developer flag
/// `--record-raw-frames`; tools/handheld/run.sh replay). In `<session>/raw/`:
///   setup.json                 the locked box (object frame), the seed point count
///   seed.f32                   the seed points (xyz float32 LE, object frame)
///   NNNNN.json                 one frame: time, camera pose, intrinsics, plane height, the phone's own result
///   NNNNN.depth / .conf / .luma / .mask   depth float32 m, confidence uint8, grey uint8, person mask uint8
enum RawFrames {
    static func writeSetup(dir: URL, box: TrackBox, seed: [SIMD3<Float>]) throws {
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let setup: [String: Any] = ["box_center": [box.center.x, box.center.y, box.center.z].map(Double.init),
                                    "box_half": [box.half.x, box.half.y, box.half.z].map(Double.init),
                                    "box_yaw": Double(box.yaw), "seed_points": seed.count, "version": 1]
        try JSONSerialization.data(withJSONObject: setup, options: [.sortedKeys]).write(to: dir.appending(path: "setup.json"))
        var flat: [Float] = []
        flat.reserveCapacity(seed.count * 3)
        for p in seed { flat += [p.x, p.y, p.z] }
        try flat.withUnsafeBytes { try Data($0).write(to: dir.appending(path: "seed.f32")) }
    }

    static func write(_ f: TrackerFrame, result: TrackResult?, index: Int, dir: URL) throws {
        let name = String(format: "%05d", index)
        var meta: [String: Any] = [
            "t": f.timestamp, "camera_to_world": flatten(f.cameraToWorld),
            "depth_w": f.depthWidth, "depth_h": f.depthHeight, "depth_k": [f.depthK.x, f.depthK.y, f.depthK.z, f.depthK.w].map(Double.init),
            "luma_w": f.luma.width, "luma_h": f.luma.height, "luma_k": [f.lumaK.x, f.lumaK.y, f.lumaK.z, f.lumaK.w].map(Double.init),
            "has_mask": f.personMask != nil,
        ]
        if let p = f.planeY { meta["plane_y"] = Double(p) }
        if let r = result {
            meta["online"] = ["object_to_world": flatten(r.objectToWorld), "quality": Double(r.quality), "state": r.state.rawValue,
                              "ms": r.ms]
        }
        try JSONSerialization.data(withJSONObject: meta, options: [.sortedKeys]).write(to: dir.appending(path: "\(name).json"))
        try f.depth.withUnsafeBytes { try Data($0).write(to: dir.appending(path: "\(name).depth")) }
        try Data(f.confidence).write(to: dir.appending(path: "\(name).conf"))
        try Data(f.luma.pixels).write(to: dir.appending(path: "\(name).luma"))
        if let m = f.personMask { try Data(m).write(to: dir.appending(path: "\(name).mask")) }
    }

    struct Setup { var box: TrackBox; var seed: [SIMD3<Float>] }

    static func readSetup(dir: URL) -> Setup? {
        guard let d = try? Data(contentsOf: dir.appending(path: "setup.json")),
              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let c = j["box_center"] as? [Double], let h = j["box_half"] as? [Double], let yaw = j["box_yaw"] as? Double,
              c.count == 3, h.count == 3 else { return nil }
        let box = TrackBox(center: SIMD3(Float(c[0]), Float(c[1]), Float(c[2])), half: SIMD3(Float(h[0]), Float(h[1]), Float(h[2])), yaw: Float(yaw))
        var seed: [SIMD3<Float>] = []
        if let raw = try? Data(contentsOf: dir.appending(path: "seed.f32")) {
            let floats = raw.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) }
            for i in stride(from: 0, to: floats.count - 2, by: 3) { seed.append(SIMD3(floats[i], floats[i + 1], floats[i + 2])) }
        }
        return Setup(box: box, seed: seed)
    }

    struct Online { var objectToWorld: simd_float4x4; var quality: Float; var state: String; var ms: Double }

    static func read(index: Int, dir: URL) -> (TrackerFrame, Online?)? {
        let name = String(format: "%05d", index)
        guard let d = try? Data(contentsOf: dir.appending(path: "\(name).json")),
              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let t = j["t"] as? Double, let m = j["camera_to_world"] as? [Double],
              let dw = j["depth_w"] as? Int, let dh = j["depth_h"] as? Int, let dk = j["depth_k"] as? [Double],
              let lw = j["luma_w"] as? Int, let lh = j["luma_h"] as? Int, let lk = j["luma_k"] as? [Double],
              let depthData = try? Data(contentsOf: dir.appending(path: "\(name).depth")),
              let conf = try? Data(contentsOf: dir.appending(path: "\(name).conf")),
              let luma = try? Data(contentsOf: dir.appending(path: "\(name).luma")) else { return nil }
        let depth = depthData.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) }
        let mask = (j["has_mask"] as? Bool ?? false) ? (try? Data(contentsOf: dir.appending(path: "\(name).mask"))).map { [UInt8]($0) } : nil
        let f = TrackerFrame(timestamp: t, cameraToWorld: unflatten(m), depth: depth, confidence: [UInt8](conf),
                             depthWidth: dw, depthHeight: dh, depthK: SIMD4(dk.map(Float.init)),
                             luma: GrayImage(width: lw, height: lh, pixels: [UInt8](luma)), lumaK: SIMD4(lk.map(Float.init)),
                             personMask: mask, planeY: (j["plane_y"] as? Double).map(Float.init))
        var online: Online?
        if let o = j["online"] as? [String: Any], let om = o["object_to_world"] as? [Double] {
            online = Online(objectToWorld: unflatten(om), quality: Float(o["quality"] as? Double ?? 0), state: o["state"] as? String ?? "",
                            ms: o["ms"] as? Double ?? 0)
        }
        return (f, online)
    }

    /// Column-major 16 numbers.
    static func flatten(_ m: simd_float4x4) -> [Double] {
        [m.columns.0, m.columns.1, m.columns.2, m.columns.3].flatMap { [Double($0.x), Double($0.y), Double($0.z), Double($0.w)] }
    }

    static func unflatten(_ v: [Double]) -> simd_float4x4 {
        let f = v.map(Float.init)
        return simd_float4x4(columns: (SIMD4(f[0], f[1], f[2], f[3]), SIMD4(f[4], f[5], f[6], f[7]),
                                       SIMD4(f[8], f[9], f[10], f[11]), SIMD4(f[12], f[13], f[14], f[15])))
    }
}
