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
    nonisolated static let prompt = "Now flip it over and give me a quick look at the bottom"
    nonisolated static let linedUp = "Lined up with the first scan"
    static let interval: Duration = .seconds(2)

    private weak var scan: ScanSession?
    private var target: FlipCloud?
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

    init(scan: ScanSession) {
        self.scan = scan
        let dir = scan.storage.dir(scan.record)
        let pkg = (try? Data(contentsOf: scan.storage.scanPackageURL(scan.record))).flatMap { try? JSON.decoder.decode(ScanPackage.self, from: $0) }
        let diag = scan.diag
        diag.log("flip_start", ["segment": scan.segment, "main_points": pkg?.pointCount ?? 0])
        guard let file = pkg?.pointsFile, let cb = pkg?.cropBox else { return }
        let box = Self.trackBox(cb)
        loop = Task { [weak self] in
            let loaded = await Task.detached(priority: .utility) { () -> FlipCloud? in
                guard let pts = ScanCloudReader.read(dir.appending(path: file)) else { return nil }
                return FlipCloud(points: pts.map(\.0), colors: pts.map { SIMD3<Float>($0.1) / 255 }, box: box)
            }.value
            self?.target = loaded
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
            let r = await Task.detached(priority: .utility) { () -> FlipAligner.Result? in
                let part = buffer.value.bindMemory(to: PointCloudRenderer.Particle.self, capacity: capacity)
                var pts: [SIMD3<Float>] = [], cols: [SIMD3<Float>] = []
                for i in 0..<capacity where part[i].positionConf.w >= 1 {
                    let p = SIMD3(part[i].positionConf.x, part[i].positionConf.y, part[i].positionConf.z)
                    guard tb.contains(p, margin: 0.005), p.y > tb.planeY + 0.004 else { continue }
                    pts.append(p)
                    cols.append(SIMD3(part[i].color.x, part[i].color.y, part[i].color.z))
                }
                guard pts.count >= 800 else { return nil }
                return FlipAligner.align(target: target, source: FlipCloud(points: pts, colors: cols, box: tb))
            }.value
            guard let r else { continue }
            let agree = r.accepted && (last.map { $0.accepted && FlipAligner.agrees($0, r, box: target.box) } ?? false)
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
        if current == "Keep walking around it" { return "Keep going around the bottom" }
        return nil
    }
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
                .font(.pillLabel)
                .foregroundStyle(.white)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(Color.black.opacity(0.5), in: Capsule())
                .overlay(Capsule().strokeBorder(Color.white.opacity(0.12), lineWidth: 0.5))
                .dynamicTypeSize(...DynamicTypeSize.accessibility1)
            }
            .buttonStyle(PressableCardStyle())
            .accessibilityHint("Saves this scan, then a quick scan of the underside")
            .transition(.opacity)
        }
    }
}
