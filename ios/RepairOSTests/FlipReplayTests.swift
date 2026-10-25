import Foundation
import simd
import Testing
@testable import RepairOS

/// Real flips replayed through the phone's code: per scan, the uploaded main-scan cloud (points_file, FlipAligner's
/// target) and the bottom segment's cloud (its own points_file: the live in-box particles the aligner sees), both
/// locked boxes, and the server's registered transform (segment_align, checked by eye). Data stays out of the repo:
/// set TEST_RUNNER_FLIP_REPLAY_DIR (xcodebuild passes it on as FLIP_REPLAY_DIR) to a folder of
/// <scan_id>/{main.ply, bottom.ply, case.json}; without it the test does nothing.
struct FlipReplayTests {
    struct Case: Decodable {
        struct Box: Decodable { let center: [Float]; let half: [Float]; let yaw: Float }
        let scan_id: String
        let main_box: Box
        let bottom_box: Box
        let server_aligned: Bool
        let server_to_segment0: [Double]?
    }

    static func box(_ b: Case.Box) -> TrackBox {
        TrackBox(center: SIMD3(b.center[0], b.center[1], b.center[2]), half: SIMD3(b.half[0], b.half[1], b.half[2]), yaw: b.yaw)
    }

    static func cloud(_ url: URL, _ box: TrackBox) -> FlipCloud? {
        guard let pts = ScanCloudReader.read(url) else { return nil }
        return FlipCloud(points: pts.map(\.0), colors: pts.map { SIMD3<Float>($0.1) / 255 }, box: box)
    }

    /// Row-major 4x4 (doubles) -> simd (column-major).
    static func matrix(_ m: [Double]) -> simd_float4x4 {
        var out = simd_float4x4()
        for r in 0..<4 { for c in 0..<4 { out[c][r] = Float(m[r * 4 + c]) } }
        return out
    }

    /// The app's old input: the box's in-box points above its floor + 4 mm.
    static func oldInput(_ c: FlipCloud) -> FlipCloud {
        var p: [SIMD3<Float>] = [], col: [SIMD3<Float>] = []
        for (i, v) in c.points.enumerated() where c.box.contains(v, margin: 0.005) && v.y > c.box.planeY + 0.004 {
            p.append(v)
            col.append(c.colors[i])
        }
        return FlipCloud(points: p, colors: col, box: c.box)
    }

    @Test func realFlipsReplayed() throws {
        guard let dir = ProcessInfo.processInfo.environment["FLIP_REPLAY_DIR"].map({ URL(fileURLWithPath: $0) }),
              let scans = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else { return }
        var accepted = 0, wrong = 0, alignable = 0, oldWrong = 0
        for scan in scans.sorted(by: { $0.path < $1.path }) {
            guard let data = try? Data(contentsOf: scan.appending(path: "case.json")) else { continue }
            let c = try JSONDecoder().decode(Case.self, from: data)
            guard let main = Self.cloud(scan.appending(path: "main.ply"), Self.box(c.main_box)),
                  let bottom = Self.cloud(scan.appending(path: "bottom.ply"), Self.box(c.bottom_box)) else { continue }
            let old = FlipAligner.align(target: Self.oldInput(main), source: Self.oldInput(bottom))
            guard let (m, mr) = FlipIsolation.isolate(main), let (b, br) = FlipIsolation.isolate(bottom) else {
                print("replay \(c.scan_id): no object isolated"); continue
            }
            if let t = c.server_to_segment0 {
                let o = FlipOutline(m)
                for s in o.starts(b, tableY: m.box.planeY) {
                    let mapped = b.points.map { Rigid.apply(s.pose, $0) }
                    print("replay \(c.scan_id) start iou \(s.iou) score \(o.score(mapped)) err \(Rigid.distance(s.pose, Self.matrix(t), probe: b.box.corners) * 1000) mm")
                }
                let mapped = b.points.map { Rigid.apply(Self.matrix(t), $0) }
                print("replay \(c.scan_id) server pose outline score \(o.score(mapped))")
            }
            let new = FlipAligner.align(target: m, source: b)
            var err: Float = -1, oldErr: Float = -1
            if let t = c.server_to_segment0, let r = old { oldErr = Rigid.distance(r.toSegment0, Self.matrix(t), probe: bottom.box.corners) * 1000 }
            if let r = old, r.accepted, !(c.server_aligned && oldErr < 30) { oldWrong += 1 }
            if let t = c.server_to_segment0, let r = new { err = Rigid.distance(r.toSegment0, Self.matrix(t), probe: b.box.corners) * 1000 }
            if c.server_aligned { alignable += 1 }
            if let r = new, r.accepted {
                if c.server_aligned && err < 30 { accepted += 1 } else { wrong += 1 }
            }
            func line(_ r: FlipAligner.Result?) -> String {
                guard let r else { return "no attempt" }
                return "\(r.refused ?? "accepted") inliers \(r.inliers) overlap \(r.overlap) rms \(r.rmsMM) mm margin \(r.margin) flip \(r.flippedDegrees) deg"
            }
            print("replay \(c.scan_id): main \(mr.dict) bottom \(br.dict)")
            print("replay \(c.scan_id): old \(line(old)) | new \(line(new)) | vs server: old \(oldErr) mm, new \(err) mm (server aligned \(c.server_aligned))")
        }
        print("replay: \(accepted)/\(alignable) the server registers accepted on the phone, \(wrong) wrong (the old input: \(oldWrong) wrong)")
        #expect(wrong == 0)
    }
}
