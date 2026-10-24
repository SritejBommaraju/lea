import Foundation
import simd

// The recreation's textured mesh in memory and on disk. Platform-neutral (Foundation + simd), so the Mac harness that
// measures it on saved scans compiles the same file.
//
// On disk (next to the scan, `<scan>/recreation/`): `shell.ply` (binary LE: float x y z s t per vertex, int triangle
// lists, `comment TextureFile shell.jpg`) + `shell.jpg` (the atlas) + `recreation.json` (RecreationManifest). The same
// PLY layout is what the gateway serves for a `textured_mesh` recreation. Frame: the scan's ARKit world, metres, Y up:
// the frame of every finding's `location.point_m`, so markers need no transform.

/// Positions, texture coordinates (0...1, origin at the atlas image's top-left, v down) and triangles.
struct TexturedMesh: Sendable {
    var positions: [SIMD3<Float>]
    var uvs: [SIMD2<Float>]
    var indices: [UInt32]

    var triangleCount: Int { indices.count / 3 }

    /// Smooth vertex normals (area-weighted), for lighting.
    func normals() -> [SIMD3<Float>] {
        var n = [SIMD3<Float>](repeating: .zero, count: positions.count)
        for t in stride(from: 0, to: indices.count, by: 3) {
            let a = Int(indices[t]), b = Int(indices[t + 1]), c = Int(indices[t + 2])
            let f = simd_cross(positions[b] - positions[a], positions[c] - positions[a])
            n[a] += f; n[b] += f; n[c] += f
        }
        // Charts duplicate a vertex where they meet: average normals over equal positions so seams shade alike.
        var byPos: [SIMD3<Float>: SIMD3<Float>] = [:]
        for (p, v) in zip(positions, n) { byPos[p, default: .zero] += v }
        return positions.map { p in
            let v = byPos[p] ?? .zero
            return simd_length(v) > 1e-12 ? simd_normalize(v) : SIMD3(0, 1, 0)
        }
    }

    /// Axis-aligned bounds.
    var bounds: (min: SIMD3<Float>, max: SIMD3<Float>) {
        var lo = SIMD3<Float>(repeating: .infinity), hi = -lo
        for p in positions { lo = simd_min(lo, p); hi = simd_max(hi, p) }
        return (lo, hi)
    }
}

enum TexturedPLY {
    enum Failure: Error { case notPLY, unsupported(String) }

    static func write(_ mesh: TexturedMesh, textureFile: String) -> Data {
        var header = "ply\nformat binary_little_endian 1.0\ncomment repairos recreation, arkit_world metres\n"
        header += "comment TextureFile \(textureFile)\nelement vertex \(mesh.positions.count)\n"
        header += ["x", "y", "z", "s", "t"].map { "property float \($0)\n" }.joined()
        header += "element face \(mesh.triangleCount)\nproperty list uchar int vertex_indices\nend_header\n"
        var data = Data(header.utf8)
        data.reserveCapacity(data.count + mesh.positions.count * 20 + mesh.triangleCount * 13)
        var vbuf = [Float]()
        vbuf.reserveCapacity(mesh.positions.count * 5)
        for (p, uv) in zip(mesh.positions, mesh.uvs) { vbuf += [p.x, p.y, p.z, uv.x, uv.y] }
        vbuf.withUnsafeBytes { data.append(contentsOf: $0) }
        var fbuf = [UInt8]()
        fbuf.reserveCapacity(mesh.triangleCount * 13)
        for t in stride(from: 0, to: mesh.indices.count, by: 3) {
            fbuf.append(3)
            for k in 0..<3 { withUnsafeBytes(of: Int32(mesh.indices[t + k]).littleEndian) { fbuf += $0 } }
        }
        data.append(contentsOf: fbuf)
        return data
    }

    /// Reads a binary LE PLY whose vertices carry float x y z and float s t (or u v / texture_u texture_v), with a
    /// triangle (or polygon, fanned) face list. Returns the mesh and the `TextureFile` comment when present.
    static func read(_ data: Data) throws -> (mesh: TexturedMesh, texture: String?) {
        guard let end = data.range(of: Data("end_header\n".utf8)) else { throw Failure.notPLY }
        let lines = String(decoding: data[data.startIndex..<end.lowerBound], as: UTF8.self).split(separator: "\n").map(String.init)
        guard lines.first == "ply", lines.contains(where: { $0.hasPrefix("format binary_little_endian") }) else { throw Failure.notPLY }
        var element = "", vCount = 0, fCount = 0, stride = 0, texture: String?
        var fields: [String: (offset: Int, size: Int)] = [:]
        var list: (count: Int, index: Int)?
        for line in lines {
            let w = line.split(separator: " ").map(String.init)
            if w.first == "comment", w.count >= 3, w[1] == "TextureFile" { texture = w[2] }
            if w.first == "element", w.count == 3 {
                element = w[1]
                if element == "vertex" { vCount = Int(w[2]) ?? 0 }
                if element == "face" { fCount = Int(w[2]) ?? 0 }
            } else if w.first == "property", element == "vertex", w.count == 3 {
                guard let size = sizes[w[1]] else { throw Failure.unsupported(w[1]) }
                fields[w[2]] = (stride, size)
                stride += size
            } else if w.first == "property", element == "face", w.count == 5, w[1] == "list" {
                list = (sizes[w[2]] ?? 1, sizes[w[3]] ?? 4)
            }
        }
        guard let x = fields["x"], let y = fields["y"], let z = fields["z"], x.size == 4 else { throw Failure.unsupported("xyz") }
        guard let s = fields["s"] ?? fields["u"] ?? fields["texture_u"], let t = fields["t"] ?? fields["v"] ?? fields["texture_v"],
              s.size == 4 else { throw Failure.unsupported("no texture coordinates") }
        guard let list, list.index == 4, fCount > 0 else { throw Failure.unsupported("faces") }
        let start = end.upperBound - data.startIndex
        guard data.count >= start + vCount * stride else { throw Failure.unsupported("truncated") }
        var mesh = TexturedMesh(positions: [], uvs: [], indices: [])
        mesh.positions.reserveCapacity(vCount)
        mesh.uvs.reserveCapacity(vCount)
        mesh.indices.reserveCapacity(fCount * 3)
        var ok = true
        data.withUnsafeBytes { raw in
            for i in 0..<vCount {
                let b = start + i * stride
                func f(_ p: (offset: Int, size: Int)) -> Float { raw.loadUnaligned(fromByteOffset: b + p.offset, as: Float.self) }
                mesh.positions.append(SIMD3(f(x), f(y), f(z)))
                mesh.uvs.append(SIMD2(f(s), f(t)))
            }
            var o = start + vCount * stride
            for _ in 0..<fCount {
                guard o + list.count <= raw.count else { ok = false; return }
                let n = list.count == 1 ? Int(raw[o]) : Int(raw.loadUnaligned(fromByteOffset: o, as: UInt32.self))
                o += list.count
                guard n >= 3, o + n * 4 <= raw.count else { ok = false; return }
                let a = raw.loadUnaligned(fromByteOffset: o, as: UInt32.self)
                for k in 1..<(n - 1) {
                    let b = raw.loadUnaligned(fromByteOffset: o + k * 4, as: UInt32.self)
                    let c = raw.loadUnaligned(fromByteOffset: o + (k + 1) * 4, as: UInt32.self)
                    if a < vCount, b < vCount, c < vCount { mesh.indices += [a, b, c] }
                }
                o += n * 4
            }
        }
        guard ok, !mesh.indices.isEmpty else { throw Failure.unsupported("faces") }
        return (mesh, texture)
    }

    private static let sizes: [String: Int] = ["char": 1, "uchar": 1, "int8": 1, "uint8": 1, "short": 2, "ushort": 2,
                                               "int16": 2, "uint16": 2, "int": 4, "uint": 4, "int32": 4, "uint32": 4,
                                               "float": 4, "float32": 4, "double": 8, "float64": 8]
}

/// `recreation.json`: what is in the folder and how it was made.
struct RecreationManifest: Codable, Sendable, Equatable {
    /// `textured_mesh` (PLY + atlas) or `splat` (a Gaussian splat file).
    var kind: String
    /// `phone` (Tier 1, built after Done) or `gateway` (Tier 2, fetched).
    var origin: String
    /// The gateway's revision (0 for the phone's own shell).
    var rev: Int
    /// The main file (the PLY or the splat), relative to the folder.
    var file: String
    /// The atlas (textured_mesh), relative to the folder.
    var texture: String?
    /// The object box's centre and radius in the scan frame, for framing the stage on the object (not its table).
    var focusCenter: [Float]?
    var focusRadius: Float?
    var stats: [String: Double] = [:]
}
