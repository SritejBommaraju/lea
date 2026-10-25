import Foundation
import simd

/// Replays a recorded hand-held scan (`<session>/raw/`, written with `--record-raw-frames`) through the tracker on
/// the Mac and compares with what the phone computed live: no ground truth on a real scan, so it reports the
/// tracker's own quality, lost/recovered events, the agreement with the phone's poses and the frame-to-frame jitter
/// while the phone held still.
enum Replay {
    static func run(dir: URL) {
        guard let setup = RawFrames.readSetup(dir: dir) else {
            print("no setup.json in \(dir.path): record a scan with --record-raw-frames first")
            return
        }
        let tracker = ObjectTracker(box: setup.box, seed: setup.seed)
        var k = 0, lost = 0, recovered = 0, diffs: [Float] = [], qs: [Float] = [], ms: [Double] = []
        var last = TrackResult.State.tracking
        // Frames in order; one missing (a partial copy off the phone) is skipped.
        let names = ((try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? [])
            .filter { $0.hasSuffix(".json") && $0 != "setup.json" }.compactMap { Int($0.dropLast(5)) }.sorted()
        for index in names {
            guard let (f, online) = RawFrames.read(index: index, dir: dir) else { continue }
            let r = tracker.process(f)
            qs.append(r.quality)
            ms.append(r.ms)
            if r.state != last {
                if r.state == .lost { lost += 1 }
                if r.state == .recovered { recovered += 1 }
                print(String(format: "  frame %d t %.2f: %@", k, f.timestamp, r.state.rawValue))
                last = r.state
            }
            if let o = online, r.state != .lost, o.state != "lost" {
                diffs.append(Rigid.distance(r.objectToWorld, o.objectToWorld, probe: setup.box.corners) * 1000)
            }
            k += 1
        }
        guard k > 0 else { print("no frames in \(dir.path)"); return }
        let sq = qs.sorted(), sm = ms.sorted(), sd = diffs.sorted()
        func pct(_ a: [Float], _ p: Double) -> Float { a.isEmpty ? 0 : a[Int(Double(a.count - 1) * p)] }
        print(String(format: "%d frames, seed %d points; quality median %.2f p10 %.2f; lost %d, recovered %d; tracker ms p50 %.1f p95 %.1f",
                     k, setup.seed.count, pct(sq, 0.5), pct(sq, 0.1), lost, recovered, sm[sm.count / 2], sm[Int(Double(sm.count - 1) * 0.95)]))
        if !sd.isEmpty {
            print(String(format: "vs the phone's live poses: median %.1f mm, p95 %.1f mm (%d frames)", pct(sd, 0.5), pct(sd, 0.95), sd.count))
        }
        print("model points \(tracker.model.count), features \(tracker.bank.count)")
    }
}
