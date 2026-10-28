import Foundation
import simd

// "Scan the bottom": the object alone, before FlipAligner lines the two scans up. The steps the server's segment_align
// took (removed with the backend reconstruction, 2026-10-28), on the phone's fused in-box cloud instead of per frame.
// Measured on four real flips (2026-10-25): the locked box is 3-4x the object and its floor sits 1-47 mm below the
// table, so "above the floor + 4 mm" kept the table: the main scan's cloud was 10-27% object and FlipAligner matched
// table to table. Here:
//   1. the table: the best-supported plane leaning at most 8 deg (the fused table leans 1-3 deg), the lowest one
//      when a lower plane is nearly as well supported (a flat top above the table is not it);
//   2. cut at the table's own noise reaching below its plane, mirrored above (nothing real lies under the table);
//   3. the connected piece (5 mm cells) nearest the box's vertical centre line, plus pieces within 1.5 cm of it;
//   4. a tight box round that piece, its floor on the measured table.

enum FlipIsolation {
    struct Report: Sendable, Equatable {
        var inputPoints = 0
        var objectPoints = 0
        var tableY: Float = 0
        var tiltDeg: Float = 0
        var cutMM: Float = 0
        /// The locked box's floor below the measured table (positive: the floor was under the table).
        var floorToTableMM: Float = 0
        var extent = SIMD3<Float>(repeating: 0)

        var dict: [String: Any] {
            ["input_points": inputPoints, "object_points": objectPoints, "table_y_m": Double(tableY).rounded(to: 4),
             "table_tilt_deg": Double(tiltDeg).rounded(to: 2), "cut_mm": Double(cutMM).rounded(to: 1),
             "floor_to_table_mm": Double(floorToTableMM).rounded(to: 1),
             "box_extent_m": [extent.x, extent.y, extent.z].map { Double($0).rounded(to: 3) }]
        }
    }

    static let maxTiltDeg: Float = 8
    static let planeTol: Float = 0.004
    static let minCut: Float = 0.003
    static let cell: Float = 0.005
    static let joinGap: Float = 0.015
    /// Below the locked box's floor kept for finding the table (the floor sat up to 47 mm off it).
    static let below: Float = 0.06

    /// The in-box points of `raw` (the box grown by 1 cm, and `below` under its floor) cut to the object, with a tight
    /// box whose floor is the table. Nil when no table or no object piece is found.
    static func isolate(_ raw: FlipCloud, seed: UInt64 = 1) -> (FlipCloud, Report)? {
        var rng = SplitMix64(seed: seed)
        let b = raw.box
        var pts: [SIMD3<Float>] = [], cols: [SIMD3<Float>] = []
        for (i, p) in raw.points.enumerated() {
            let l = b.local(p)
            guard abs(l.x) <= b.half.x + 0.01, abs(l.z) <= b.half.z + 0.01, l.y >= -b.half.y - below, l.y <= b.half.y + 0.01 else { continue }
            pts.append(p)
            cols.append(i < raw.colors.count ? raw.colors[i] : SIMD3(repeating: 0.5))
        }
        var report = Report(inputPoints: pts.count)
        guard pts.count >= 300 else { return nil }
        let sample = pts.count > 20_000 ? (0..<20_000).map { _ in pts[Int(rng.next() % UInt64(pts.count))] } : pts
        guard var (support, plane) = fitTable(sample, &rng) else { return nil }
        while true {   // a lower plane nearly as well supported is the table
            let lower = sample.filter { simd_dot(plane.n, $0) + plane.d < -0.01 }
            guard lower.count >= 200, let (k, pl) = fitTable(lower, &rng), Float(k) >= 0.5 * Float(support) else { break }
            (support, plane) = (k, pl)
        }
        plane = refine(sample, plane)
        let cut = mirroredCut(pts, plane)
        var obj: [SIMD3<Float>] = [], objCol: [SIMD3<Float>] = []
        for (i, p) in pts.enumerated() where simd_dot(plane.n, p) + plane.d > cut {
            obj.append(p)
            objCol.append(cols[i])
        }
        guard obj.count >= 100, let keep = centralPieces(obj, centre: b.center) else { return nil }
        let o = keep.map { obj[$0] }, oc = keep.map { objCol[$0] }
        let box = tightBox(o, plane: plane)
        report.objectPoints = o.count
        report.tableY = plane.height(atX: box.center.x, z: box.center.z)
        report.tiltDeg = acos(min(1, plane.n.y)) * 180 / .pi
        report.cutMM = cut * 1000
        report.floorToTableMM = (report.tableY - b.planeY) * 1000
        report.extent = box.half * 2
        return (FlipCloud(points: o, colors: oc, box: box), report)
    }

    /// What the table's noise reaches below its plane (99.5th percentile within 3 cm), mirrored above; at least
    /// `minCut`.
    static func mirroredCut(_ pts: [SIMD3<Float>], _ plane: Plane) -> Float {
        var under: [Float] = []
        for p in pts {
            let h = simd_dot(plane.n, p) + plane.d
            if h < 0, h > -0.03 { under.append(-h) }
        }
        guard under.count >= 100 else { return minCut }
        under.sort()
        return max(minCut, under[min(under.count - 1, Int(Float(under.count) * 0.995))])
    }

    struct Plane {
        var n: SIMD3<Float>
        var d: Float
        func height(atX x: Float, z: Float) -> Float { -(n.x * x + n.z * z + d) / n.y }
    }

    /// Random-sample consensus over planes leaning at most `maxTiltDeg`: (inliers within `planeTol`, plane).
    static func fitTable(_ q: [SIMD3<Float>], _ rng: inout SplitMix64, iters: Int = 200) -> (Int, Plane)? {
        guard q.count >= 3 else { return nil }
        let cmin = cos(maxTiltDeg * .pi / 180)
        var best: (Int, Plane)?
        for _ in 0..<iters {
            let a = q[Int(rng.next() % UInt64(q.count))], b = q[Int(rng.next() % UInt64(q.count))], c = q[Int(rng.next() % UInt64(q.count))]
            var n = simd_cross(b - a, c - a)
            let len = simd_length(n)
            guard len > 1e-9 else { continue }
            n /= len
            if n.y < 0 { n = -n }
            guard n.y >= cmin else { continue }
            let d = -simd_dot(n, a)
            var k = 0
            for p in q where abs(simd_dot(n, p) + d) < planeTol { k += 1 }
            if k > (best?.0 ?? 0) { best = (k, Plane(n: n, d: d)) }
        }
        return best
    }

    /// Least squares on the inliers: y = a x + b z + c.
    static func refine(_ q: [SIMD3<Float>], _ p: Plane) -> Plane {
        var m = simd_double3x3(), r = SIMD3<Double>(repeating: 0)
        var count = 0
        for v in q where abs(simd_dot(p.n, v) + p.d) < planeTol {
            let x = SIMD3<Double>(Double(v.x), Double(v.z), 1)
            m += simd_double3x3(rows: [x * x.x, x * x.y, x * x.z])
            r += x * Double(v.y)
            count += 1
        }
        guard count >= 30, abs(m.determinant) > 1e-12 else { return p }
        let s = m.inverse * r   // y = s.x x + s.y z + s.z
        var n = SIMD3<Float>(Float(-s.x), 1, Float(-s.y))
        let len = simd_length(n)
        n /= len
        return Plane(n: n, d: Float(-s.z) / len)
    }

    /// Indices of the points in the connected piece (26-connected `cell` cells) whose points come nearest the box's
    /// vertical centre line, plus pieces within `joinGap` of it; specks under 2% are ignored.
    static func centralPieces(_ p: [SIMD3<Float>], centre: SIMD3<Float>) -> [Int]? {
        func key(_ v: SIMD3<Float>) -> SIMD3<Int32> { SIMD3(Int32((v.x / cell).rounded(.down)), Int32((v.y / cell).rounded(.down)), Int32((v.z / cell).rounded(.down))) }
        var cellOf = [SIMD3<Int32>: Int](), cells: [SIMD3<Int32>] = []
        var pointCell = [Int](repeating: 0, count: p.count)
        for (i, v) in p.enumerated() {
            let k = key(v)
            if let c = cellOf[k] { pointCell[i] = c } else { cellOf[k] = cells.count; pointCell[i] = cells.count; cells.append(k) }
        }
        var label = [Int](repeating: -1, count: cells.count)
        var pieces = 0
        for start in 0..<cells.count where label[start] < 0 {
            var stack = [start]
            label[start] = pieces
            while let c = stack.popLast() {
                let k = cells[c]
                for dx: Int32 in -1...1 { for dy: Int32 in -1...1 { for dz: Int32 in -1...1 {
                    if let m = cellOf[k &+ SIMD3(dx, dy, dz)], label[m] < 0 { label[m] = pieces; stack.append(m) }
                } } }
            }
            pieces += 1
        }
        var count = [Int](repeating: 0, count: pieces), near = [Float](repeating: .infinity, count: pieces)
        var lo = [SIMD3<Float>](repeating: SIMD3(repeating: .infinity), count: pieces)
        var hi = [SIMD3<Float>](repeating: SIMD3(repeating: -.infinity), count: pieces)
        for (i, v) in p.enumerated() {
            let l = label[pointCell[i]]
            count[l] += 1
            near[l] = min(near[l], simd_length(SIMD2(v.x - centre.x, v.z - centre.z)))
            lo[l] = simd_min(lo[l], v)
            hi[l] = simd_max(hi[l], v)
        }
        let minSize = max(20, p.count / 50)
        let big = (0..<pieces).filter { count[$0] >= minSize }
        guard let core = big.min(by: { near[$0] < near[$1] }) else { return nil }
        var keep: Set<Int> = [core]
        for l in big where l != core {
            let gap = simd_max(SIMD3(repeating: 0), simd_max(lo[l] - hi[core], lo[core] - hi[l]))
            if simd_length(gap) <= joinGap { keep.insert(l) }
        }
        return p.indices.filter { keep.contains(label[pointCell[$0]]) }
    }

    /// Upright box round the points: heading from their spread seen from above, sides at the 1st/99th percentiles
    /// plus 1 cm, floor on the table under its centre.
    static func tightBox(_ p: [SIMD3<Float>], plane: Plane) -> TrackBox {
        let n = Float(p.count)
        let mx = p.reduce(Float(0)) { $0 + $1.x } / n, mz = p.reduce(Float(0)) { $0 + $1.z } / n
        var cxx: Float = 0, czz: Float = 0, cxz: Float = 0
        for v in p { cxx += (v.x - mx) * (v.x - mx); czz += (v.z - mz) * (v.z - mz); cxz += (v.x - mx) * (v.z - mz) }
        let angle = 0.5 * atan2(2 * cxz, cxx - czz)            // major axis (cos, sin) in x, z
        let yaw = atan2(-sin(angle), cos(angle))               // TrackBox: its x axis is (cos yaw, 0, -sin yaw)
        let probe = TrackBox(center: SIMD3(mx, 0, mz), half: SIMD3(repeating: 1), yaw: yaw)
        let loc = p.map { probe.local($0) }
        func pct(_ a: [Float], _ q: Float) -> Float { let s = a.sorted(); return s[min(s.count - 1, max(0, Int(Float(s.count - 1) * q)))] }
        let lx = loc.map(\.x), lz = loc.map(\.z)
        let x0 = pct(lx, 0.01) - 0.01, x1 = pct(lx, 0.99) + 0.01, z0 = pct(lz, 0.01) - 0.01, z1 = pct(lz, 0.99) + 0.01
        let cl = SIMD3<Float>((x0 + x1) / 2, 0, (z0 + z1) / 2)
        let r = simd_quatf(angle: yaw, axis: SIMD3(0, 1, 0))
        let c = SIMD3(mx, 0, mz) + r.act(cl)
        let floor = plane.height(atX: c.x, z: c.z)
        let top = pct(p.map(\.y), 0.995) + 0.01
        return TrackBox(center: SIMD3(c.x, (floor + top) / 2, c.z), half: SIMD3((x1 - x0) / 2, (top - floor) / 2, (z1 - z0) / 2), yaw: yaw)
    }
}

private extension Double {
    func rounded(to places: Int) -> Double {
        let m = pow(10, Double(places))
        return (self * m).rounded() / m
    }
}
