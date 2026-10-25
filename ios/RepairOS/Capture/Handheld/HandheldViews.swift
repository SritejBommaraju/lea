import SwiftUI
import simd

extension ScanSession {
    /// Starts "Turn it" on the locked object: from now on the object may move; the object frame is the world frame
    /// of this moment (the object still rests where it was locked).
    func startHandheld() {
        guard handheld == nil, let box, stage == .scanning || stage == .locking else { return }
        let seed = renderer?.points(in: box).map(\.0).filter { $0.y > box.planeY + TrackerConfig.standard.tableClearance } ?? []
        let raw = CommandLine.arguments.contains("--record-raw-frames") ? storage.dir(record).appending(path: "raw") : nil
        let h = HandheldController(box: box, seed: seed, planeY: box.planeY, renderer: renderer, diag: diag, rawDir: raw)
        h.configure(session)
        handheld = h
    }
}

/// The scan screen's "Turn it" choice (developer flag `--turn-it`): before Scan, a toggle; while scanning a small
/// object from one spot, a one-tap suggestion. Plain words, the capture flow's own chip look.
struct HandheldModeChip: View {
    /// Developer flag only (`--turn-it`): in-hand turning is not the product flow ("Scan the bottom" is).
    static let enabled = CommandLine.arguments.contains("--turn-it")
    @Bindable var scan: ScanSession
    @Binding var wanted: Bool
    @State private var suggest = false
    @State private var path: [(t: Date, p: SIMD3<Float>)] = []

    var body: some View {
        Group {
            if !Self.enabled || scan.handheld != nil || scan.continuing {
                EmptyView()   // a continuation lives in the first visit's world frame: walk-around only
            } else if scan.stage == .ready || scan.stage == .locking {
                chip(wanted ? "Turning it in your hand" : "Turn it in your hand", symbol: wanted ? "checkmark" : "rotate.3d") {
                    wanted.toggle()
                }
                .accessibilityHint("Scan a small object by turning it instead of walking around it")
            } else if scan.stage == .scanning, suggest {
                chip("Small? Turn it in your hand", symbol: "rotate.3d") {
                    scan.diag.log("handheld_suggestion_taken", ["path_m": pathLength])
                    scan.startHandheld()
                }
                .transition(.opacity.combined(with: .scale(scale: 0.9)))
            }
        }
        .motion(Motion.fade, value: suggest)
        .task(id: scan.stage) { await watch() }
        .onAppear { if CommandLine.arguments.contains("--turn-it") { wanted = true } }   // developer: start in Turn it
    }

    private func chip(_ text: String, symbol: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: symbol).imageScale(.small)
                Text(text).lineLimit(1)
            }
            .font(.pillLabel)
            .foregroundStyle(.white)
            .padding(.horizontal, 12).padding(.vertical, 8)
            .background(Color.black.opacity(0.5), in: Capsule())
            .overlay(Capsule().strokeBorder(Color.white.opacity(0.12), lineWidth: 0.5))
            .dynamicTypeSize(...DynamicTypeSize.accessibility1)
        }
        .buttonStyle(PressableCardStyle())
    }

    private var pathLength: Double {
        guard path.count > 1 else { return 0 }
        return Double(zip(path, path.dropFirst()).map { simd_distance($0.p, $1.p) }.reduce(0, +))
    }

    /// Suggest it when the locked object is small (under ~25 cm) and, 5 s into the scan, the phone has moved under
    /// 25 cm in total with few sides covered: the person is scanning from one spot.
    private func watch() async {
        guard scan.stage == .scanning else { return }
        let start = Date()
        path = []
        while !Task.isCancelled, scan.stage == .scanning, scan.handheld == nil {
            if let m = scan.session.currentFrame?.camera.transform {
                path.append((Date(), SIMD3(m.columns.3.x, m.columns.3.y, m.columns.3.z)))
                path.removeAll { Date().timeIntervalSince($0.t) > 8 }
            }
            let small = scan.box.map { max($0.size.x, $0.size.y, $0.size.z) < 0.25 } ?? false
            let show = small && Date().timeIntervalSince(start) > 5 && pathLength < 0.25 && scan.dome.count < 6
            if show != suggest {
                suggest = show
                if show { scan.diag.log("handheld_suggested", ["path_m": pathLength, "covered": scan.dome.count]) }
            }
            try? await Task.sleep(for: .milliseconds(500))
        }
        suggest = false
    }
}

/// Symbols and urgency for the hand-held cues (CueStyle's table covers the walk-around ones).
enum HandheldCues {
    static let table: [String: (String, CueBanner.Tone)] = [
        "Hold it up and turn it slowly": ("rotate.3d", .guide),
        "Keep turning it slowly": ("rotate.3d", .guide),
        "Turn it more slowly": ("tortoise.fill", .caution),
        "Hold it still for a moment": ("hand.raised.fill", .caution),
        "Now flip it over": ("arrow.up.arrow.down", .guide),
        "Set it down and point at it": ("viewfinder", .guide),
        FlipSegmentController.prompt: ("arrow.up.arrow.down", .guide),
        FlipSegmentController.linedUp: ("checkmark", .good),
        "Keep going around the bottom": ("figure.walk", .guide),
        FlipSegmentController.sidesCue: ("cube.transparent", .guide),
    ]
}
