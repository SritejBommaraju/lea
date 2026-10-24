// Offline harness for the hand-held tracker (ios/RepairOS/Capture/Handheld), built and run on the Mac:
//   tools/handheld/run.sh synthetic box|cylinder [frames] [hz] [rate]   synthetic scene, pose error vs ground truth
//   tools/handheld/run.sh lost box|cylinder                               object hidden mid-turn: lost → recovered
//   tools/handheld/run.sh replay <session dir>/raw                        a recorded session (--record-raw-frames)
import Foundation
import simd

let args = CommandLine.arguments
func arg(_ i: Int, _ d: String) -> String { i < args.count ? args[i] : d }

func summary(_ name: String, _ r: SyntheticRun) {
    let ms = r.rows.map(\.ms).sorted()
    let p95 = ms.isEmpty ? 0 : ms[Int(Double(ms.count - 1) * 0.95)]
    let q = r.rows.map(\.quality)
    print(String(format: "%@: %d tracked frames, turned %.0f°, error mean %.2f mm / %.2f°, max %.2f mm / %.2f°, quality mean %.2f, tracker ms mean %.1f p95 %.1f",
                 name, r.rows.count, r.turnedDeg, r.meanMM, r.meanDeg, r.maxMM, r.maxDeg,
                 q.isEmpty ? 0 : q.reduce(0, +) / Float(q.count), ms.isEmpty ? 0 : ms.reduce(0, +) / Double(ms.count), p95))
    let sure = r.rows.filter { $0.quality >= 0.7 }
    if !sure.isEmpty {
        print(String(format: "  frames reported quality >= 0.7: %d, error mean %.2f mm / %.2f°, max %.2f mm / %.2f°", sure.count,
                     sure.map(\.mm).reduce(0, +) / Float(sure.count), sure.map(\.deg).reduce(0, +) / Float(sure.count),
                     sure.map(\.mm).max() ?? 0, sure.map(\.deg).max() ?? 0))
    }
}

switch arg(1, "synthetic") {
case "synthetic":
    var scene = SyntheticScene(arg(2, "box") == "box" ? .texturedBox : .cylinderWithPatch)
    if args.contains("--unmasked-hand") { scene.maskHand = false }
    if args.contains("--noise1") { scene.noise = 0.001 }
    let frames = Int(arg(3, "90")) ?? 90, hz = Double(arg(4, "15")) ?? 15, rate = Float(arg(5, "0.8")) ?? 0.8
    let verbose = args.contains("-v")
    let r = SyntheticRun.run(scene, hz: hz, frames: frames, rate: rate) { k, res in
        if verbose {
            let gt = SyntheticScene.objectPose(Double(k) / hz, box: scene.box, rate: rate)
            let e = scene.error(res.objectToWorld, gt)
            print(String(format: "%3d %@ q %.2f err %.1f mm %.1f° pts %d inl %d rms %.1f feat %d/%d/%d %@ cond %.3f model %d bank %d %.0f ms",
                         k, res.state.rawValue, res.quality, e.mm, e.deg, res.points, res.inliers, res.rmsMM, res.featuresSeen,
                         res.featureMatches, res.featureInliers, res.start, res.conditioning, res.modelPoints, res.bankFeatures, res.ms))
        }
    }
    summary("\(arg(2, "box")) @\(hz) Hz", r)
case "lost":
    let scene = SyntheticScene(arg(2, "box") == "box" ? .texturedBox : .cylinderWithPatch)
    var events: [String] = []
    var last = TrackResult.State.tracking
    let r = SyntheticRun.run(scene, frames: 90, occlude: 45..<53) { k, res in
        if res.state != last { events.append("\(k):\(res.state.rawValue)"); last = res.state }
        if args.contains("-v") {
            let gt = SyntheticScene.objectPose(Double(k) / 15, box: scene.box)
            let e = scene.error(res.objectToWorld, gt)
            print(String(format: "%3d %@ q %.2f err %.1f mm %.1f° pts %d feat %d/%d/%d %@ %.0f ms", k, res.state.rawValue, res.quality, e.mm, e.deg,
                         res.points, res.featuresSeen, res.featureMatches, res.featureInliers, res.start, res.ms))
        }
    }
    print("events:", events.joined(separator: " "))
    summary("lost+recovered \(arg(2, "box"))", r)
case "flip":
    // Flip + mini-scan alignment: success rate and error over random flips.
    let shape: SyntheticFlip.Shape = arg(2, "box") == "box" ? .texturedBox : .flatBoard
    let trials = Int(arg(3, "20")) ?? 20
    var ok = 0, wrong = 0, refused: [String: Int] = [:], errs: [Float] = [], ms: [Double] = []
    for k in 0..<trials {
        let c = SyntheticFlip.make(shape, seed: UInt64(100 + k), flipAboutZ: k % 2 == 1)
        guard let r = FlipAligner.align(target: c.target, source: c.source) else { refused["none", default: 0] += 1; continue }
        let e = SyntheticFlip.error(r.toSegment0, c) * 1000
        ms.append(r.ms)
        if args.contains("-v") {
            let d = r.toSegment0 * Rigid.inverse(c.truth)
            let dt = Rigid.apply(r.toSegment0, c.source.box.center) - Rigid.apply(c.truth, c.source.box.center)
            print(String(format: "   rot err %.1f° centre offset (%.1f, %.1f, %.1f) mm", Rigid.angle(d, Rigid.identity) * 180 / .pi, dt.x * 1000, dt.y * 1000, dt.z * 1000))
            print(String(format: "%2d err %6.1f mm q %.2f overlap %.2f rms %.1f inl %d margin %.3f flip %.0f° %@ %.0f ms", k, e, r.quality,
                         r.overlap, r.rmsMM, r.inliers, r.margin, r.flippedDegrees, r.refused ?? "accepted", r.ms))
        }
        if let why = r.refused { refused[why, default: 0] += 1 } else if e < 10 { ok += 1; errs.append(e) } else { wrong += 1 }
    }
    let mean = errs.isEmpty ? 0 : errs.reduce(0, +) / Float(errs.count)
    print(String(format: "flip %@: %d/%d aligned (error mean %.2f mm, max %.2f mm), %d wrong accepted, refused %@, ms mean %.0f",
                 arg(2, "box"), ok, trials, mean, errs.max() ?? 0, wrong, refused.description, ms.isEmpty ? 0 : ms.reduce(0, +) / Double(ms.count)))
case "replay":
    Replay.run(dir: URL(fileURLWithPath: arg(2, ".")))
default:
    print("usage: synthetic|lost|replay")
}
