import CoreGraphics
import Foundation
import ImageIO
import simd

// Tier 1: the instant textured shell, built on the phone right after Done from what the capture folder already holds
// (`scans/<id>/scan_package.json` keyframes with LiDAR depth, the 12 MP stills with poses, the locked box). Reads only
// files, so the Mac harness runs it on saved scans. Writes `<scanDir>/recreation/phone/` (shell.ply, shell.jpg,
// recreation.json). Platform-neutral: Foundation, simd, CoreGraphics, ImageIO.

enum ShellBuilder {
    enum Failure: Error, CustomStringConvertible {
        case noPackage, noBox, noDepth, noSurface, noViews, noTexture, write(String)
        var description: String {
            switch self {
            case .noPackage: "no scan package"
            case .noBox: "the scan has no locked object box"
            case .noDepth: "the scan has no keyframes with LiDAR depth"
            case .noSurface: "no surface in the object's region"
            case .noViews: "no posed photos"
            case .noTexture: "texturing failed"
            case .write(let m): "could not write the recreation: \(m)"
            }
        }
    }

    static let folder = "recreation/phone"
    static let atlasSize = 4096
    static let maxKeyframes = 80

    /// Builds the shell for `scanID` in `sessionDir` and returns its manifest (already written next to the files, in
    /// `outputDir`, default `<scanDir>/recreation/phone`).
    static func build(sessionDir: URL, scanID: String, atlasSize: Int = atlasSize, outputDir: URL? = nil) throws -> RecreationManifest {
        let started = Date()
        var seconds: [String: Double] = [:]
        let scanDir = sessionDir.appending(path: "scans/\(scanID)")
        guard let data = try? Data(contentsOf: scanDir.appending(path: "scan_package.json")),
              let pkg = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw Failure.noPackage }
        guard let box = box(pkg["crop_box"]) else { throw Failure.noBox }
        let segments = sameFrameSegments(pkg)
        var t0 = Date()
        let keyframes = ((pkg["keyframes"] as? [[String: Any]]) ?? []).filter { segments.contains(($0["segment"] as? Int) ?? 0) }
        let frames: [DepthFrameData] = keyframes.compactMap { depthFrame($0, base: sessionDir) }
        guard !frames.isEmpty else { throw Failure.noDepth }
        seconds["load_depth_s"] = Date().timeIntervalSince(t0)
        guard let surface = ShellSurface.build(frames: frames, box: box) else { throw Failure.noSurface }
        for (k, v) in surface.seconds { seconds["surface_" + k] = v }
        t0 = Date()

        // Views: the stills (same frame, reliable pose), then keyframes thinned evenly as fallbacks.
        var views: [ShellView] = []
        let stillsDir = sessionDir.appending(path: "stills")
        let names = ((try? FileManager.default.contentsOfDirectory(atPath: stillsDir.path)) ?? [])
            .filter { $0.hasPrefix("\(scanID)_s") && $0.hasSuffix(".json") }.sorted()
        for name in names {
            guard let d = try? Data(contentsOf: stillsDir.appending(path: name)),
                  let s = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                  segments.contains((s["segment"] as? Int) ?? 0), (s["pose_reliable"] as? Bool) != false,
                  let pose = pose(s["camera"]), let file = s["image_file"] as? String else { continue }
            let url = sessionDir.appending(path: file)
            guard FileManager.default.fileExists(atPath: url.path) else { continue }
            var w = s["width_px"] as? Int ?? 0, h = s["height_px"] as? Int ?? 0
            if w == 0 || h == 0, let (iw, ih) = ShellTexture.imageSize(url) { w = iw; h = ih }
            guard let cam = intrinsics(s["intrinsics"], width: w, height: h) else { continue }
            var depth: DepthFrameData?
            if let df = s["depth_file"] as? String {
                depth = depthFrame(["depth_file": df, "confidence_file": s["confidence_file"] as Any,
                                    "camera": s["camera"] as Any, "intrinsics": s["intrinsics"] as Any],
                                   base: sessionDir, imageSize: (w, h))
            }
            views.append(ShellView(name: s["still_id"] as? String ?? name, isStill: true, image: url, pose: pose, cam: cam, depth: depth))
        }
        let step = max(1, Int((Double(keyframes.count) / Double(maxKeyframes)).rounded(.up)))
        for (i, kf) in keyframes.enumerated() where i % step == 0 {
            guard let file = kf["image_file"] as? String, let pose = pose(kf["camera"]),
                  let cam = intrinsics(kf["intrinsics"], width: nil, height: nil) else { continue }
            let url = sessionDir.appending(path: file)
            guard FileManager.default.fileExists(atPath: url.path) else { continue }
            views.append(ShellView(name: kf["keyframe_id"] as? String ?? file, isStill: false, image: url, pose: pose, cam: cam,
                                   depth: depthFrame(kf, base: sessionDir), weight: ShellTexture.keyframePenalty))
        }
        guard !views.isEmpty else { throw Failure.noViews }
        // Relative sharpness per kind (1 = the median of its kind), in parallel.
        let sharp = SharedBuffer([Float](repeating: 1, count: views.count))
        let vs = views
        DispatchQueue.concurrentPerform(iterations: vs.count) { i in sharp.a[i] = ShellTexture.sharpness(vs[i], box: box) }
        for still in [true, false] {
            let idx = views.indices.filter { views[$0].isStill == still }
            let med = idx.map { sharp.a[$0] }.sorted()[safe: idx.count / 2] ?? 1
            for i in idx { views[i].sharpness = min(2, max(0.25, sharp.a[i] / max(med, 1e-6))) }
        }
        seconds["views_s"] = Date().timeIntervalSince(t0)

        guard let out = ShellTexture.texture(positions: surface.positions, indices: surface.indices, views: views,
                                             voxel: surface.voxel, atlasSize: atlasSize) else { throw Failure.noTexture }
        t0 = Date()
        let dir = outputDir ?? scanDir.appending(path: folder)
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            guard let jpeg = ShellTexture.jpeg(out.atlas) else { throw Failure.write("jpeg") }
            try jpeg.write(to: dir.appending(path: "shell.jpg"), options: .atomic)
            try TexturedPLY.write(out.mesh, textureFile: "shell.jpg").write(to: dir.appending(path: "shell.ply"), options: .atomic)
            seconds["write_s"] = Date().timeIntervalSince(t0)
            seconds["texture_bytes"] = Double(jpeg.count)
        } catch {
            throw Failure.write(error.localizedDescription)
        }
        var stats = out.stats
        for (k, v) in seconds { stats[k] = v }
        stats["total_s"] = Date().timeIntervalSince(started)
        stats["voxel_mm"] = Double(surface.voxel * 1000)
        stats["triangles"] = Double(out.mesh.triangleCount)
        stats["vertices"] = Double(out.mesh.positions.count)
        stats["keyframes"] = Double(frames.count)
        stats["atlas_px"] = Double(atlasSize)
        stats["footprint_mb"] = memoryFootprintMB()
        let focus = objectFocus(surface.positions, groundY: surface.groundY, box: box)
        let manifest = RecreationManifest(kind: "textured_mesh", origin: "phone", rev: 0, file: "shell.ply", texture: "shell.jpg",
                                          focusCenter: [focus.center.x, focus.center.y, focus.center.z], focusRadius: focus.radius,
                                          stats: stats)
        do {
            let enc = JSONEncoder()
            enc.outputFormatting = [.sortedKeys]
            try enc.encode(manifest).write(to: dir.appending(path: "recreation.json"), options: .atomic)
        } catch {
            throw Failure.write(error.localizedDescription)
        }
        return manifest
    }

    /// What the stage frames: the surface above the table inside the box footprint (2nd to 98th percentile bounds),
    /// so the object fills the view even when the locked box was generous; the box itself when too little stands up.
    static func objectFocus(_ v: [SIMD3<Float>], groundY: Float?, box: ShellBox) -> (center: SIMD3<Float>, radius: Float) {
        let floor = (groundY ?? (box.center.y - box.extent.y / 2)) + max(0.01, 0.1 * box.extent.y)
        let pts = v.filter { p in
            let l = box.local(p)
            return p.y > floor && abs(l.x) <= box.extent.x * 0.55 && abs(l.z) <= box.extent.z * 0.55
        }
        guard pts.count >= 50 else { return (box.center, box.radius) }
        var lo = SIMD3<Float>(), hi = SIMD3<Float>()
        for axis in 0..<3 {
            let s = pts.map { $0[axis] }.sorted()
            lo[axis] = s[s.count * 2 / 100]
            hi[axis] = s[min(s.count - 1, s.count * 98 / 100)]
        }
        lo.y = min(lo.y, groundY ?? lo.y)          // the object stands on the table: frame from the table up
        return ((lo + hi) / 2, max(0.02, simd_length(hi - lo) / 2))
    }

    // MARK: Parsing

    static func sameFrameSegments(_ pkg: [String: Any]) -> Set<Int> {
        var s: Set<Int> = [0]
        for seg in (pkg["segments"] as? [[String: Any]]) ?? [] where seg["reason"] as? String == "continued" && (seg["aligned"] as? Bool) != false {
            if let n = seg["segment"] as? Int { s.insert(n) }
        }
        return s
    }

    static func floats(_ any: Any?) -> [Float]? { (any as? [NSNumber])?.map(\.floatValue) }

    static func box(_ any: Any?) -> ShellBox? {
        guard let b = any as? [String: Any], let c = floats(b["center_m"]), c.count == 3, let e = floats(b["extent_m"]), e.count == 3
        else { return nil }
        let q = floats(b["quaternion_xyzw"]) ?? [0, 0, 0, 1]
        let r = simd_float3x3(simd_quatf(ix: q[0], iy: q[1], iz: q[2], r: q[3]).normalized)
        return ShellBox(center: SIMD3(c[0], c[1], c[2]), extent: SIMD3(e[0], e[1], e[2]), rotation: r)
    }

    static func pose(_ any: Any?) -> CameraPose? {
        guard let c = any as? [String: Any], let p = floats(c["position_m"]), p.count == 3,
              let q = floats(c["quaternion_xyzw"]), q.count == 4 else { return nil }
        if let frame = c["frame"] as? String, frame != "arkit_world" { return nil }
        return CameraPose(position: SIMD3(p[0], p[1], p[2]), quaternionXYZW: q)
    }

    static func intrinsics(_ any: Any?, width: Int?, height: Int?) -> Intrinsics? {
        guard let i = any as? [String: Any], let fx = (i["fx"] as? NSNumber)?.floatValue, let fy = (i["fy"] as? NSNumber)?.floatValue,
              let cx = (i["cx"] as? NSNumber)?.floatValue, let cy = (i["cy"] as? NSNumber)?.floatValue,
              let w = (i["width_px"] as? Int) ?? width, let h = (i["height_px"] as? Int) ?? height, w > 0, h > 0 else { return nil }
        return Intrinsics(fx: fx, fy: fy, cx: cx, cy: cy, width: w, height: h)
    }

    /// A record's LiDAR depth (uint16 mm PNG) + confidence, with the depth grid's intrinsics (the record's intrinsics
    /// scaled to the grid; `imageSize` for a still, whose intrinsics carry no size).
    static func depthFrame(_ r: [String: Any], base: URL, imageSize: (Int, Int)? = nil) -> DepthFrameData? {
        guard let df = r["depth_file"] as? String, let pose = pose(r["camera"]),
              let (mm, w, h) = gray16(base.appending(path: df)),
              let full = intrinsics(r["intrinsics"], width: imageSize?.0, height: imageSize?.1) else { return nil }
        let conf = (r["confidence_file"] as? String).flatMap { gray8(base.appending(path: $0)) }
        return DepthFrameData(pose: pose, cam: full.scaled(to: w, h), width: w, height: h,
                              depth: mm.map { Float($0) / 1000 }, confidence: conf?.0.count == w * h ? conf?.0 : nil)
    }

    /// A 16-bit grey PNG's values (ImageIO keeps 16-bit grey; the byte order follows the image's bitmap info).
    static func gray16(_ url: URL) -> ([UInt16], Int, Int)? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let img = CGImageSourceCreateImageAtIndex(src, 0, [kCGImageSourceShouldCache: false] as CFDictionary),
              img.bitsPerComponent == 16, img.bitsPerPixel == 16, let data = img.dataProvider?.data as Data? else { return nil }
        let w = img.width, h = img.height, row = img.bytesPerRow
        let little = img.bitmapInfo.contains(.byteOrder16Little)
        var out = [UInt16](repeating: 0, count: w * h)
        data.withUnsafeBytes { raw in
            for y in 0..<h { for x in 0..<w {
                let v = raw.loadUnaligned(fromByteOffset: y * row + x * 2, as: UInt16.self)
                out[y * w + x] = little ? UInt16(littleEndian: v) : UInt16(bigEndian: v)
            } }
        }
        return (out, w, h)
    }

    static func gray8(_ url: URL) -> ([UInt8], Int, Int)? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let img = CGImageSourceCreateImageAtIndex(src, 0, nil),
              img.bitsPerComponent == 8, img.bitsPerPixel == 8, let data = img.dataProvider?.data as Data? else { return nil }
        let w = img.width, h = img.height, row = img.bytesPerRow
        var out = [UInt8](repeating: 0, count: w * h)
        data.withUnsafeBytes { raw in for y in 0..<h { for x in 0..<w { out[y * w + x] = raw[y * row + x] } } }
        return (out, w, h)
    }

    /// This process's physical memory footprint (what jetsam counts), MB.
    static func memoryFootprintMB() -> Double {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
        let kr = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count) }
        }
        return kr == KERN_SUCCESS ? Double(info.phys_footprint) / 1_048_576 : -1
    }
}

extension Array {
    subscript(safe i: Int) -> Element? { indices.contains(i) ? self[i] : nil }
}
