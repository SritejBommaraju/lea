import Foundation

/// Binary little-endian PLY in metres, Y up: xyz float32, plus rgb uint8 when colours are given (the scan-package
/// `points_file`; the gateway checks the header and the vertex count).
enum PLYWriter {
    @discardableResult
    static func write(_ points: [SIMD3<Float>], colors: [SIMD3<UInt8>]? = nil, to url: URL) throws -> Int {
        let rgb = colors.map { $0.count == points.count } ?? false
        var header = "ply\nformat binary_little_endian 1.0\ncomment RepairOS; metres; Y up\nelement vertex \(points.count)\n"
        header += "property float x\nproperty float y\nproperty float z\n"
        if rgb { header += "property uchar red\nproperty uchar green\nproperty uchar blue\n" }
        header += "end_header\n"
        var data = Data(header.utf8)
        data.reserveCapacity(data.count + points.count * (rgb ? 15 : 12))
        for (i, p) in points.enumerated() {
            withUnsafeBytes(of: p.x.bitPattern.littleEndian) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: p.y.bitPattern.littleEndian) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: p.z.bitPattern.littleEndian) { data.append(contentsOf: $0) }
            if rgb, let c = colors?[i] { data.append(contentsOf: [c.x, c.y, c.z]) }
        }
        try data.write(to: url, options: .atomic)
        return points.count
    }
}

/// Reads back the PLY `PLYWriter` writes (xyz float32, optional rgb uint8).
enum ScanCloudReader {
    static func read(_ url: URL) -> [(SIMD3<Float>, SIMD3<UInt8>)]? {
        guard let data = try? Data(contentsOf: url), let end = data.range(of: Data("end_header\n".utf8))?.upperBound else { return nil }
        let header = String(decoding: data[..<end], as: UTF8.self)
        guard header.contains("binary_little_endian"),
              let line = header.split(separator: "\n").first(where: { $0.hasPrefix("element vertex") }),
              let n = Int(line.split(separator: " ").last ?? "") else { return nil }
        let rgb = header.contains("property uchar red")
        let stride = rgb ? 15 : 12
        guard data.count - end >= n * stride else { return nil }
        return data[end...].withUnsafeBytes { raw in
            (0..<n).map { i in
                let o = i * stride
                func f(_ k: Int) -> Float { Float(bitPattern: UInt32(littleEndian: raw.loadUnaligned(fromByteOffset: o + k, as: UInt32.self))) }
                let c = rgb ? SIMD3(raw[o + 12], raw[o + 13], raw[o + 14]) : SIMD3<UInt8>(200, 200, 200)
                return (SIMD3(f(0), f(4), f(8)), c)
            }
        }
    }
}
