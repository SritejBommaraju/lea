import Foundation
import SwiftUI
import simd

/// "Scan the bottom": after the main scan, the person flips the object over and gives the phone a quick look at
/// the underside (10–20 s). It is a new capture segment (reason flipped, object_moved) in its own ARKit session:
/// the object moved, so the room's map is not used. While it runs, the mini-scan's in-box cloud is registered to the
/// main scan's saved cloud every ~2 s on a background queue (FlipAligner, full 6 DoF); two attempts in a row that
/// agree line it up: the main scan's cloud appears around the flipped object, Lea says so, and the segment records
/// `transform_to_segment0` with its quality. Unsure: the scan goes on and the backend registers it. Never blocks
/// capture.
@MainActor
final class FlipSegmentController {
    /// Upside down or on a side, whichever it sits on steadily (a mouse rocks upside down; the server tries every resting
    /// face), on a firm surface, sides too: the server lines the two scans up by the sides both saw, and a soft surface
    /// squashes round the object (the owner's Xbox flip on a padded stool could not be lined up, 2026-10-25).
    nonisolated static let prompt = "Turn it over or onto a side, whichever way it sits steadily on a firm surface, then scan the bottom and its sides"
    nonisolated static let linedUp = "Lined up with the first scan"
    /// Not lined up yet after `sidesAfter`: the sides are what both scans share; after `lowerAfter`, more firmly.
    nonisolated static let sidesCue = "Get the sides too, so I can line it up"
    nonisolated static let lowerCue = "Lower the phone: I need the sides"
    /// A flat object (a board, a plate) shares only its rim when it lies face down.
    nonisolated static let flatCue = "Lay it on an edge, or keep the rim and sides in view"
    nonisolated static let sidesAfter: TimeInterval = 8
    nonisolated static let lowerAfter: TimeInterval = 20
    /// The main scan's locked box at most this tall: a flat object (its two big faces never overlap).
    nonisolated static let flatHeight: Float = 0.045
    static let interval: Duration = .seconds(2)

    private weak var scan: ScanSession?
    private var target: FlipCloud?
    private var flat = false
    private var log = FlipLog()
    private var last: FlipAligner.Result?
    private(set) var result: FlipAligner.Result?
    private var attempts = 0
    private var linedUpAt: Date?
    private var loop: Task<Void, Never>?
    private let started = Date()

    var aligned: Bool { result != nil }
    var transformQuality: Double? { result.map { (Double($0.quality) * 100).rounded() / 100 } }
    /// Row-major 4 × 4, metres: this segment → segment 0.
    var transformRowMajor: [Double]? {
        result.map { r in (0..<4).flatMap { row in (0..<4).map { col in Double(r.toSegment0[col][row]) } } }
    }
    /// The segment's `flip_align` record (numbers only, uploaded with the scan for diagnosis).
    var flipAlign: FlipAlign? { log.contract }

    init(scan: ScanSession) {
        self.scan = scan
        let dir = scan.storage.dir(scan.record)
        let pkg = (try? Data(contentsOf: scan.storage.scanPackageURL(scan.record))).flatMap { try? JSON.decoder.decode(ScanPackage.self, from: $0) }
        let diag = scan.diag
        diag.log("flip_start", ["segment": scan.segment, "main_points": pkg?.pointCount ?? 0])
        guard let file = pkg?.pointsFile, let cb = pkg?.cropBox else { return }
        let box = Self.trackBox(cb)
        flat = Self.isFlat(box)
        loop = Task { [weak self] in
            // The main scan's cloud cut to the object (FlipIsolation): its box was 3-4x the object on real flips.
            let loaded = await Task.detached(priority: .utility) { () -> (FlipCloud, FlipIsolation.Report)? in
                guard let pts = ScanCloudReader.read(dir.appending(path: file)) else { return nil }
                return FlipIsolation.isolate(FlipCloud(points: pts.map(\.0), colors: pts.map { SIMD3<Float>($0.1) / 255 }, box: box))
            }.value
            self?.target = loaded?.0
            self?.log.main = loaded?.1
            diag.log("flip_target", loaded.map { $0.1.dict } ?? ["isolated": false])
            await self?.run()
        }
    }

    func stop() { loop?.cancel() }

    static func trackBox(_ cb: CropBox) -> TrackBox {
        let q = simd_quatf(ix: Float(cb.quaternionXyzw[0]), iy: Float(cb.quaternionXyzw[1]), iz: Float(cb.quaternionXyzw[2]),
                           r: Float(cb.quaternionXyzw[3]))
        return TrackBox(center: SIMD3(cb.centerM.map(Float.init)), half: SIMD3(cb.extentM.map(Float.init)) / 2,
                        yaw: 2 * atan2(q.imag.y, q.real))
    }

    /// Every ~2 s once the bottom is locked: copy its in-box points off the GPU buffer and try to line it up.
    private func run() async {
        while !Task.isCancelled {
            try? await Task.sleep(for: Self.interval)
            guard let scan, scan.stage == .scanning || scan.stage == .locking else {
                if scan == nil || scan?.stage == .finishing { return }
                continue
            }
            guard result == nil, let target, let renderer = scan.renderer, let box = scan.box else { continue }
            let tb = TrackBox(center: box.center, half: box.size / 2, yaw: box.yaw)
            let buffer = Unchecked(value: renderer.particles.contents())
            let capacity = PointCloudRenderer.maxPoints
            attempts += 1
            let attempt = attempts
            let below = FlipIsolation.below
            let out = await Task.detached(priority: .utility) { () -> (FlipAligner.Result?, FlipIsolation.Report?, Int) in
                let part = buffer.value.bindMemory(to: PointCloudRenderer.Particle.self, capacity: capacity)
                var pts: [SIMD3<Float>] = [], cols: [SIMD3<Float>] = []
                for i in 0..<capacity where part[i].positionConf.w >= 1 {
                    let p = SIMD3(part[i].positionConf.x, part[i].positionConf.y, part[i].positionConf.z)
                    // The table too (under the box's floor): the isolation fits it and cuts it away.
                    let l = tb.local(p)
                    guard abs(l.x) <= tb.half.x + 0.01, abs(l.z) <= tb.half.z + 0.01, l.y >= -tb.half.y - below, l.y <= tb.half.y + 0.01 else { continue }
                    pts.append(p)
                    cols.append(SIMD3(part[i].color.x, part[i].color.y, part[i].color.z))
                }
                guard pts.count >= 800, let (source, rep) = FlipIsolation.isolate(FlipCloud(points: pts, colors: cols, box: tb)) else {
                    return (nil, nil, pts.count)
                }
                return (FlipAligner.align(target: target, source: source), rep, source.points.count)
            }.value
            if let rep = out.1 { log.bottom = rep }
            guard let r = out.0 else {
                log.add(attempt: attempt, result: nil, points: out.2, agrees: false)
                continue
            }
            let agree = r.accepted && (last.map { $0.accepted && FlipAligner.agrees($0, r, box: target.box) } ?? false)
            log.add(attempt: attempt, result: r, points: out.2, agrees: agree)
            scan.diag.log("flip_align", ["attempt": attempt, "align_ms": Int(r.ms), "inliers": r.inliers, "rms_mm": (Double(r.rmsMM) * 10).rounded() / 10,
                                         "overlap": (Double(r.overlap) * 100).rounded() / 100, "margin": (Double(r.margin) * 1000).rounded() / 1000,
                                         "quality": (Double(r.quality) * 100).rounded() / 100, "flip_deg": Int(r.flippedDegrees),
                                         "refused": r.refused ?? "", "agrees": agree, "since_start_s": Int(Date().timeIntervalSince(started))])
            last = r
            if agree {
                result = r
                linedUpAt = Date()
                showMerged(r, renderer: renderer)
                LeaVoice.shared.dynamicCue(Self.linedUp, diag: scan.diag)
                scan.diag.log("flip_aligned", ["attempt": attempt, "quality": (Double(r.quality) * 100).rounded() / 100])
            }
        }
    }

    /// The main scan's cloud, moved onto the flipped object, drawn with the live one (never exported with it).
    private func showMerged(_ r: FlipAligner.Result, renderer: PointCloudRenderer) {
        guard let target else { return }
        let toLive = Rigid.inverse(r.toSegment0)
        let pts = zip(target.points, target.colors).map { p, c in
            (Rigid.apply(toLive, p), SIMD3<UInt8>(simd_clamp(c, .zero, SIMD3(repeating: 1)) * 255))
        }
        _ = renderer.seed(pts, confidence: 0.5)
    }

    /// The flipped object's coverage cells are its underside: recorded as b<ring>s<sector>.
    func cellID(_ cell: Int) -> String { String(format: "b%ds%02d", cell / CoverageDome.sectors, cell % CoverageDome.sectors) }

    func cue(replacing current: String?) -> String? {
        if let at = linedUpAt, Date().timeIntervalSince(at) < 3 { return Self.linedUp }
        return Self.steadyCue(replacing: current, aligned: aligned, elapsed: Date().timeIntervalSince(started), flat: flat)
    }

    /// The carry-on cue during the bottom scan, until it has lined up: the sides after a few seconds, then more firmly
    /// (lower the phone); a flat object is asked to stand on an edge or show its rim. Lined up: the bottom.
    nonisolated static func steadyCue(replacing current: String?, aligned: Bool, elapsed: TimeInterval, flat: Bool = false) -> String? {
        guard current == "Keep walking around it" else { return nil }
        if aligned { return "Keep going around the bottom" }
        if flat { return elapsed >= sidesAfter / 2 ? flatCue : "Keep going around the bottom" }
        if elapsed >= lowerAfter { return lowerCue }
        return elapsed >= sidesAfter ? sidesCue : "Keep going around the bottom"
    }

    /// A flat object by its locked box: the board, plate or phone kind, whose two big faces never overlap.
    nonisolated static func isFlat(_ box: TrackBox) -> Bool { box.half.y * 2 <= flatHeight }
}

/// "Scan the bottom" (the scanner after the main scan has covered some sides, and the report): finish what is being
/// scanned, then open the mini-scan of the bottom.
struct ScanBottomAction {
    let run: (SessionRecord?) -> Void
    func callAsFunction(_ finished: SessionRecord? = nil) { run(finished) }
}

extension EnvironmentValues {
    @Entry var scanBottom: ScanBottomAction? = nil
}

struct ScanBottomChip: View {
    @Bindable var scan: ScanSession
    @Environment(\.scanBottom) private var scanBottom
    @State private var busy = false

    var body: some View {
        if let scanBottom, scan.stage == .scanning, !scan.continuing, scan.box != nil, scan.dome.count >= 6 || scan.stills.count >= 6 {
            Button {
                guard !busy else { return }
                busy = true
                Task {
                    let r = await scan.finish()
                    scanBottom(r)
                }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "arrow.up.arrow.down").imageScale(.small)
                    Text("Scan the bottom").lineLimit(1)
                }
                .cameraChip()
            }
            .buttonStyle(PressableCardStyle())
            .accessibilityHint("Saves this scan, then a quick scan of the underside")
            .transition(.opacity)
        }
    }
}

/// The bottom scan's `flip_align` record: how each cloud was cut to the object and the last few attempts (which check
/// refused, with its numbers). Small on purpose: numbers only, at most `keep` attempts.
struct FlipLog {
    static let keep = 6
    var attempts = 0
    var main: FlipIsolation.Report?
    var bottom: FlipIsolation.Report?
    var last: [FlipAttempt] = []

    mutating func add(attempt: Int, result r: FlipAligner.Result?, points: Int, agrees: Bool) {
        attempts = attempt
        func round(_ x: Float, _ places: Double) -> Double { (Double(x) * pow(10, places)).rounded() / pow(10, places) }
        let a = r.map { r in
            FlipAttempt(agrees: agrees, attempt: attempt, flipDeg: round(r.flippedDegrees, 0), inliers: r.inliers,
                        margin: round(r.margin, 3), ms: Int(r.ms), overlap: round(r.overlap, 3), points: points,
                        quality: round(r.quality, 2), refused: r.refused, rmsMm: round(r.rmsMM, 2))
        } ?? FlipAttempt(agrees: false, attempt: attempt, flipDeg: nil, inliers: nil, margin: nil, ms: nil, overlap: nil,
                         points: points, quality: nil, refused: "isolation", rmsMm: nil)
        last = Array((last + [a]).suffix(Self.keep))
    }

    static func cut(_ r: FlipIsolation.Report?) -> FlipCut? {
        guard let r else { return nil }
        func round(_ x: Float, _ places: Double) -> Double { (Double(x) * pow(10, places)).rounded() / pow(10, places) }
        return FlipCut(boxExtentM: [r.extent.x, r.extent.y, r.extent.z].map { round($0, 3) }, cutMm: round(r.cutMM, 1),
                       floorToTableMm: round(r.floorToTableMM, 1), inputPoints: r.inputPoints, objectPoints: r.objectPoints,
                       tableTiltDeg: round(r.tiltDeg, 2), tableYM: round(r.tableY, 4))
    }

    var contract: FlipAlign? {
        guard attempts > 0 || main != nil else { return nil }
        return FlipAlign(attempts: attempts, bottom: Self.cut(bottom), last: last.isEmpty ? nil : last, main: Self.cut(main))
    }
}
