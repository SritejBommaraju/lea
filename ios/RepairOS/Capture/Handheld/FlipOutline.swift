import Foundation
import simd

// The outline seen from above (FlipAligner's footprint starts and checks, as the server's segment_align): a flipped
// object shares little surface with its first scan, but its outline from above is the same, mirrored by the flip.
// For each resting pose (upside down, each side) and heading, the mini-scan's top-view silhouette is laid on the main
// scan's; the best headings become starts, put down on the table. Every candidate is also checked against it: the
// main scan saw the whole outline from above, so a pose that puts the mini-scan outside it is wrong.

struct FlipOutline {
    static let cell: Float = 0.004
    static let size = 128              // 51 cm square around the main scan's object
    let origin: SIMD2<Float>
    let filled: [Bool]                 // the main scan's silhouette, small holes closed
    let grown: [Bool]                  // ... grown by 1 cm (drift)
    let count: Int

    init(_ target: FlipCloud) {
        var c = SIMD2<Float>(0, 0)
        for p in target.points { c += SIMD2(p.x, p.z) }
        c /= Float(max(1, target.points.count))
        origin = c - SIMD2(repeating: Float(Self.size) * Self.cell / 2)
        let raw = Self.occupancy(target.points.map { SIMD2($0.x, $0.z) }, origin: origin)
        filled = Self.close(raw)
        grown = Self.dilate(filled, 3)
        count = filled.filter { $0 }.count
    }

    @inline(__always) static func index(_ q: SIMD2<Float>, origin: SIMD2<Float>) -> Int? {
        let i = Int(((q.x - origin.x) / cell).rounded(.down)), j = Int(((q.y - origin.y) / cell).rounded(.down))
        guard i >= 0, i < size, j >= 0, j < size else { return nil }
        return i * size + j
    }

    static func occupancy(_ q: [SIMD2<Float>], origin: SIMD2<Float>) -> [Bool] {
        var g = [Bool](repeating: false, count: size * size)
        for v in q { if let k = index(v, origin: origin) { g[k] = true } }
        return g
    }

    static func dilate(_ g: [Bool], _ r: Int) -> [Bool] {
        var out = g
        for i in 0..<size { for j in 0..<size where g[i * size + j] {
            for a in max(0, i - r)...min(size - 1, i + r) { for b in max(0, j - r)...min(size - 1, j + r) { out[a * size + b] = true } }
        } }
        return out
    }

    static func erode(_ g: [Bool], _ r: Int) -> [Bool] { dilate(g.map { !$0 }, r).map { !$0 } }
    static func close(_ g: [Bool]) -> [Bool] { erode(dilate(g, 2), 2) }

    /// Overlap (IoU) of mapped mini-scan points' silhouette with the main scan's, and the share of them outside the
    /// main scan's silhouette grown by 1 cm.
    func score(_ mapped: [SIMD3<Float>]) -> (iou: Float, outside: Float) {
        guard !mapped.isEmpty else { return (0, 1) }
        let g = Self.close(Self.occupancy(mapped.map { SIMD2($0.x, $0.z) }, origin: origin))
        var inter = 0, union = 0
        for k in 0..<g.count { if g[k] && filled[k] { inter += 1 }; if g[k] || filled[k] { union += 1 } }
        var out = 0
        for p in mapped { if let k = Self.index(SIMD2(p.x, p.z), origin: origin), grown[k] { continue }; out += 1 }
        return (union > 0 ? Float(inter) / Float(union) : 0, Float(out) / Float(mapped.count))
    }

    static let resting: [(String, simd_float3x3)] = [
        ("flipped", simd_float3x3(diagonal: SIMD3(1, -1, -1))),
        ("side_px", simd_float3x3(rows: [SIMD3(1, 0, 0), SIMD3(0, 0, -1), SIMD3(0, 1, 0)])),
        ("side_nx", simd_float3x3(rows: [SIMD3(1, 0, 0), SIMD3(0, 0, 1), SIMD3(0, -1, 0)])),
        ("side_pz", simd_float3x3(rows: [SIMD3(0, -1, 0), SIMD3(1, 0, 0), SIMD3(0, 0, 1)])),
        ("side_nz", simd_float3x3(rows: [SIMD3(0, 1, 0), SIMD3(-1, 0, 0), SIMD3(0, 0, 1)])),
    ]

    /// Starts (mini-scan -> main scan) whose outline from above best matches, `perPose` distinct headings (20 deg
    /// apart) per resting pose, each put down on the main scan's table at `tableY` (its lowest points on it).
    func starts(_ source: FlipCloud, tableY: Float, perPose: Int = 2, stepDeg: Float = 4) -> [(pose: simd_float4x4, iou: Float)] {
        let step = max(1, source.points.count / 2500)
        let src = stride(from: 0, to: source.points.count, by: step).map { source.points[$0] }
        guard src.count >= 50, count > 0 else { return [] }
        let tc = origin + SIMD2(repeating: Float(Self.size) * Self.cell / 2)
        let ci = Self.size / 2, cj = Self.size / 2
        var out: [(simd_float4x4, Float)] = []
        for (_, base) in Self.resting {
            var scored: [(Float, Float, simd_float4x4)] = []
            var deg: Float = 0
            while deg < 360 {
                let r = simd_float3x3(simd_quatf(angle: deg * .pi / 180, axis: SIMD3(0, 1, 0))) * base
                let q = src.map { r * $0 }
                var c = SIMD2<Float>(0, 0)
                for v in q { c += SIMD2(v.x, v.z) }
                c /= Float(q.count)
                var cells = Set<SIMD2<Int32>>()
                for v in q { cells.insert(SIMD2(Int32(((v.x - c.x) / Self.cell).rounded(.down)), Int32(((v.z - c.y) / Self.cell).rounded(.down)))) }
                // Centre on centre, then the best shift within 2 cm.
                var best: (Float, Int, Int) = (-1, 0, 0)
                for si in -5...5 { for sj in -5...5 {
                    var hits = 0
                    for k in cells {
                        let i = ci + Int(k.x) + si, j = cj + Int(k.y) + sj
                        if i >= 0, i < Self.size, j >= 0, j < Self.size, filled[i * Self.size + j] { hits += 1 }
                    }
                    let iou = Float(hits) / Float(max(1, cells.count + count - hits))
                    if iou > best.0 { best = (iou, si, sj) }
                } }
                let t = tc - c + SIMD2(Float(best.1), Float(best.2)) * Self.cell
                let ys = q.map(\.y).sorted()
                let y0 = ys[min(ys.count - 1, ys.count / 100)]
                scored.append((best.0, deg, Rigid.make(r, SIMD3(t.x, tableY - y0, t.y))))
                deg += stepDeg
            }
            scored.sort { $0.0 > $1.0 }
            var kept: [(Float, Float, simd_float4x4)] = []
            for s in scored where kept.allSatisfy({ min(abs($0.1 - s.1), 360 - abs($0.1 - s.1)) >= 20 }) {
                kept.append(s)
                if kept.count >= perPose { break }
            }
            out += kept.map { ($0.2, $0.0) }
        }
        return out
    }
}
