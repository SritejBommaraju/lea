import ARKit
import CoreImage
import SwiftUI
import UIKit

/// One place Lea pointed at, as fractions of the photo she was shown (0,0 top left).
struct LeaMark: Sendable, Equatable {
    var x: Double
    var y: Double
    var label: String
}

/// What Lea sees and points at while the person scans (VEL's look-and-draw, on the camera instead of a screen).
/// Each turn can carry the camera frame as a photo; Lea answers with marks in that photo's coordinates, and each mark
/// is pinned to the object in 3D (LiDAR depth from the moment the photo was taken), so the circle stays on it as the
/// person walks around. A long press is the person's own pointer: it is pinned the same way and drawn into the next
/// photo as a magenta ring ("they mean the ring"). A finished scan's findings are circled too while continuing it.
@MainActor
@Observable
final class LeaSight {
    enum Kind: Equatable { case lea, person, finding }

    struct Pin: Identifiable, Equatable {
        let id = UUID()
        var world: SIMD3<Float>
        var label: String
        var kind: Kind
        let born = Date()
    }

    private(set) var pins: [Pin] = []
    /// Lea may take photos now: a conversation window is open (from a turn's start until a little after her
    /// answer). Shown as "Lea is looking"; no photo is ever taken outside it, so none before "Hey Lea".
    private(set) var looking = false

    /// Where the person pointed, on the object (world): projected into the next photo Lea sees, wherever the phone is
    /// by then (they point, then talk, often while moving).
    @ObservationIgnored private var pointedAt: SIMD3<Float>?
    /// The camera at the moment of the last photo sent: its marks are pinned from here, wherever the phone is now.
    @ObservationIgnored private var lastLook: Snapshot?
    /// Puts a photo on the gateway (PUT /v1/blobs), returning its ref; set by the scan screen.
    @ObservationIgnored var uploader: (@Sendable (Data) async -> String?)?
    /// The keyframe ring: the last few photos inside the window, each uploading as soon as it is taken. The newest
    /// is "now" once a turn has started.
    @ObservationIgnored private var shots: [Shot] = []
    /// A turn has started and its "now" photo is taken: no keyframe may come after it until the turn's photos go.
    @ObservationIgnored private var turnShot = false
    @ObservationIgnored private var windowEnds: Task<Void, Never>?

    struct Snapshot {
        let transform: simd_float4x4
        let intrinsics: simd_float3x3
        let resolution: CGSize
        /// LiDAR depth of that moment (Capture/DepthAndPoints), nil without LiDAR.
        let depth: DepthCopy?
    }

    /// One photo for Lea: its upload (started when taken), its camera, and whether the person's ring is in it.
    struct Shot {
        let at: Date
        let snapshot: Snapshot
        let pointed: Bool
        let upload: Task<String?, Never>
    }

    nonisolated static let longSide: CGFloat = 768
    nonisolated static let jpegQuality: CGFloat = 0.65
    /// At most this many photos per turn (the gateway takes up to 3, oldest first, the last one now).
    static let maxShots = 3
    /// A keyframe older than this is not "a few seconds ago" any more: it is dropped.
    static let shotMaxAge: TimeInterval = 20
    /// How long the window stays open after Lea's answer (the follow-up), unless another turn starts.
    static let windowAfterAnswer: TimeInterval = 12
    /// The longest a turn keeps it open without an answer (a tap, a dropped turn, a yes that took an offer).
    static let windowForTurn: TimeInterval = 30
    nonisolated private static let ciContext = CIContext(options: [.cacheIntermediates: false])

    // MARK: The conversation window

    /// A turn is starting (Ink-2 heard speech, the talk button, typing): the window opens and the camera now is taken
    /// and uploaded while the person talks. Once per turn.
    func turnStarted(_ frame: ARFrame?) {
        openWindow(until: Date().addingTimeInterval(Self.windowForTurn))
        guard !turnShot, let frame else { return }
        turnShot = true
        take(frame, withPointer: true)
    }

    /// The turn's photos, oldest first (the last one now; taken here if the turn did not start with one), their
    /// uploads awaited (long done by now, normally). Their camera becomes the one Lea's marks are pinned from.
    func photosForTurn(_ frame: ARFrame?) async -> (refs: [String], pointed: Bool, depth: Bool)? {
        if !turnShot, let frame { turnStarted(frame) }
        turnShot = false
        let fresh = shots.filter { Date().timeIntervalSince($0.at) <= Self.shotMaxAge }.suffix(Self.maxShots)
        guard let now = fresh.last else { return nil }
        var refs: [String] = []
        for s in fresh { if let ref = await s.upload.value { refs.append(ref) } }
        guard !refs.isEmpty else { return nil }
        lastLook = now.snapshot
        return (refs, now.pointed, now.snapshot.depth != nil)
    }

    /// Lea finished answering: the window stays open a little for the follow-up, then closes.
    func answerEnded() { openWindow(until: Date().addingTimeInterval(Self.windowAfterAnswer)) }

    /// The window closes: photos stop and the ring is forgotten.
    func closeWindow() {
        windowEnds?.cancel()
        looking = false
        turnShot = false
        shots = []
    }

    private func openWindow(until end: Date) {
        looking = true
        windowEnds?.cancel()
        windowEnds = Task { [weak self] in
            try? await Task.sleep(for: .seconds(max(0, end.timeIntervalSinceNow)))
            if !Task.isCancelled { self?.closeWindow() }
        }
    }

    // MARK: Keyframes inside the window

    /// From the scan's frame loop: a keyframe when the window is open, no turn photo is pending, and the view is
    /// sharp and new (KeyframeRule).
    func offer(_ frame: ARFrame, sharpness: Double, medianSharpness: Double?) {
        guard looking, !turnShot, uploader != nil else { return }
        let last = shots.last.map { ($0.snapshot.transform, $0.at) }
        guard KeyframeRule.accept(last: last, pose: frame.camera.transform, at: Date(), sharpness: sharpness,
                                  medianSharpness: medianSharpness) else { return }
        take(frame, withPointer: false)
    }

    private func take(_ frame: ARFrame, withPointer: Bool) {
        // The ring goes where the pointed spot is in this frame; none when it has gone out of view.
        let pointed = withPointer ? pointedAt.flatMap {
            Self.sensorPoint(of: $0, transform: frame.camera.transform, intrinsics: frame.camera.intrinsics,
                             resolution: frame.camera.imageResolution)
        } : nil
        if withPointer { pointedAt = nil }
        guard let jpeg = Self.jpeg(CIImage(cvPixelBuffer: frame.capturedImage), ring: pointed.map(Self.portrait)),
              let uploader else { return }
        let shot = Shot(at: Date(), snapshot: Self.snapshot(frame), pointed: pointed != nil,
                        upload: Task { await uploader(jpeg) })
        shots = Array((shots + [shot]).suffix(Self.maxShots))
    }

    // MARK: The photo

    /// The photo Lea sees: the sensor image turned upright (portrait), `longSide` pixels on its long side at scale 1
    /// (a screen-scale renderer would make it 3x as big), JPEG at `jpegQuality`, the person's pointer drawn in as a
    /// magenta ring at `ring` (fractions of the photo).
    nonisolated static func jpeg(_ sensor: CIImage, ring: CGPoint? = nil) -> Data? {
        var image = sensor.oriented(.right)
        let scale = longSide / max(image.extent.width, image.extent.height)
        image = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        let rect = CGRect(origin: .zero, size: CGSize(width: image.extent.width.rounded(), height: image.extent.height.rounded()))
        image = image.transformed(by: CGAffineTransform(translationX: -image.extent.minX, y: -image.extent.minY))
        guard let cg = ciContext.createCGImage(image, from: rect) else { return nil }
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = true
        return UIGraphicsImageRenderer(size: rect.size, format: format).jpegData(withCompressionQuality: jpegQuality) { _ in
            UIImage(cgImage: cg).draw(in: rect)
            if let u = ring {
                let r = rect.width * 0.06
                let path = UIBezierPath(ovalIn: CGRect(x: u.x * rect.width - r, y: u.y * rect.height - r, width: 2 * r, height: 2 * r))
                path.lineWidth = max(3, r * 0.18)
                UIColor.magenta.setStroke()
                path.stroke()
            }
        }
    }

    // MARK: Measuring

    /// The distance between two points of the last photo (fractions of it), from that moment's LiDAR depth: what
    /// Lea says, and both ends pinned. Without depth at either end it says so; it never estimates.
    func measure(_ a: CGPoint, _ b: CGPoint) -> String {
        guard let look = lastLook, let depth = look.depth else { return "I can't measure that here: there's no depth reading in this view." }
        guard let p = depth.worldPoint(at: Self.sensor(a)), let q = depth.worldPoint(at: Self.sensor(b)) else {
            return "I can't measure that: there's no depth reading at those points. Try a little farther back."
        }
        let mm = Self.millimetres(p, q)
        let words = mm >= 1000 ? String(format: "%.2f metres", Double(mm) / 1000) : "\(mm) millimetres"
        pins = pins.filter { $0.kind != .lea } + [Pin(world: p, label: "\(mm) mm", kind: .lea), Pin(world: q, label: "", kind: .lea)]
        return "That's about \(words), from the LiDAR."
    }

    nonisolated static func millimetres(_ p: SIMD3<Float>, _ q: SIMD3<Float>) -> Int { Int((simd_distance(p, q) * 1000).rounded()) }

    /// Lea's marks for the last photo: pinned in 3D, replacing her earlier marks (the person's and the findings stay).
    func pin(_ marks: [LeaMark], session: ARSession) {
        guard let look = lastLook, !marks.isEmpty else { return }
        let placed = marks.compactMap { m -> Pin? in
            let s = Self.sensor(CGPoint(x: m.x, y: m.y))
            return Self.world(at: s, look, session: session).map { Pin(world: $0, label: m.label, kind: .lea) }
        }
        pins = pins.filter { $0.kind != .lea } + placed
    }

    // MARK: The person points

    /// A long press on the camera view: pinned on the object, and drawn into the next photo Lea sees.
    @discardableResult
    func point(at viewPoint: CGPoint, viewSize: CGSize, frame: ARFrame, session: ARSession) -> Bool {
        guard viewSize.width > 0, viewSize.height > 0 else { return false }
        let inv = frame.displayTransform(for: .portrait, viewportSize: viewSize).inverted()
        let s = CGPoint(x: viewPoint.x / viewSize.width, y: viewPoint.y / viewSize.height).applying(inv)
        guard (0...1).contains(s.x), (0...1).contains(s.y),
              let w = Self.world(at: s, Self.snapshot(frame), session: session) else { return false }   // nothing there
        pointedAt = w
        pins = pins.filter { $0.kind != .person } + [Pin(world: w, label: "You pointed", kind: .person)]
        return true
    }

    // MARK: Findings while continuing a scan

    /// The report's top findings with a computed place on the scan (same ARKit frame once relocalised).
    func show(findings: [(SIMD3<Float>, String)]) {
        pins = pins.filter { $0.kind != .finding } + findings.map { Pin(world: $0.0, label: $0.1, kind: .finding) }
    }

    /// For Lea: what is circled on screen now.
    var circled: String? {
        let words = pins.map { $0.kind == .person ? "the person's pointer" : $0.label }.filter { !$0.isEmpty }
        return words.isEmpty ? nil : words.joined(separator: ", ")
    }

    func clear() { pins = []; pointedAt = nil; lastLook = nil; closeWindow() }

    /// ARKit restarted tracking with another origin (a continued scan relocalised, or gave up and started fresh): pins
    /// made in the old frame would point at the wrong place, and an answer still on its way was about the old frame.
    /// The report's findings stay: they are in the saved map's frame and only drawn once the scan is aligned.
    func trackingReset() {
        pins = pins.filter { $0.kind == .finding }
        pointedAt = nil
        lastLook = nil
        shots = []   // their cameras are in the old frame
    }

    // MARK: Geometry

    /// Where a world point is in a camera's image (normalized sensor-image coordinates), or nil when it is behind the
    /// camera or outside the image. The inverse of `world(at:)`.
    nonisolated static func sensorPoint(of world: SIMD3<Float>, transform: simd_float4x4, intrinsics k: simd_float3x3,
                                        resolution: CGSize) -> CGPoint? {
        let c = transform.inverse * SIMD4(world, 1)
        guard c.z < -0.01 else { return nil }
        let d = -c.z
        let s = CGPoint(x: Double((c.x / d * k[0][0] + k[2][0]) / Float(resolution.width)),
                        y: Double((-c.y / d * k[1][1] + k[2][1]) / Float(resolution.height)))
        return (0...1).contains(s.x) && (0...1).contains(s.y) ? s : nil
    }

    /// Portrait photo fraction → normalized sensor-image point (the photo is the sensor image turned right).
    nonisolated static func sensor(_ portrait: CGPoint) -> CGPoint { CGPoint(x: portrait.y, y: 1 - portrait.x) }

    /// Normalized sensor-image point → portrait photo fraction.
    nonisolated static func portrait(_ sensor: CGPoint) -> CGPoint { CGPoint(x: 1 - sensor.y, y: sensor.x) }

    private static func snapshot(_ frame: ARFrame) -> Snapshot {
        Snapshot(transform: frame.camera.transform, intrinsics: frame.camera.intrinsics,
                 resolution: frame.camera.imageResolution, depth: DepthCopy.from(frame))
    }

    /// The world point under a normalized sensor-image point: LiDAR depth at that moment, else a raycast on any
    /// surface ARKit estimates (phones without LiDAR). Nil when neither finds one.
    static func world(at s: CGPoint, _ look: Snapshot, session: ARSession) -> SIMD3<Float>? {
        if let p = look.depth?.worldPoint(at: s) { return p }
        let k = look.intrinsics
        let ray = cameraRay(fx: k[0][0], fy: k[1][1], cx: k[2][0], cy: k[2][1],
                            x: Float(s.x * look.resolution.width), y: Float(s.y * look.resolution.height))
        let m = look.transform
        let origin = SIMD3(m.columns.3.x, m.columns.3.y, m.columns.3.z)
        let d4 = m * SIMD4(simd_normalize(ray), 0)
        let query = ARRaycastQuery(origin: origin, direction: SIMD3(d4.x, d4.y, d4.z), allowing: .estimatedPlane, alignment: .any)
        guard let hit = session.raycast(query).first else { return nil }
        return SIMD3(hit.worldTransform.columns.3.x, hit.worldTransform.columns.3.y, hit.worldTransform.columns.3.z)
    }
}

/// When a frame is worth keeping as one of Lea's keyframes: sharp (not under 70 % of the session's running median
/// sharpness), new (moved about 8 cm or turned about 10 degrees since the last), and at most one per 2 s.
enum KeyframeRule {
    static let interval: TimeInterval = 2
    static let move: Float = 0.08
    static let turn: Float = 10 * .pi / 180
    static let sharpnessRatio = 0.7

    static func accept(last: (pose: simd_float4x4, at: Date)?, pose: simd_float4x4, at now: Date, sharpness: Double,
                       medianSharpness: Double?) -> Bool {
        if let m = medianSharpness, m > 0, sharpness < m * sharpnessRatio { return false }
        guard let last else { return true }
        guard now.timeIntervalSince(last.at) >= interval else { return false }
        return ScanSession.moved(from: last.pose, to: pose, move: move, turn: turn)
    }
}

/// Lea's eyes for a conversation (LeaConversation.eyes): photos only inside the conversation window.
@MainActor
protocol LeaEyes: AnyObject {
    /// A turn is starting: the camera now is taken and uploaded while the person talks.
    func turnStarted()
    /// This turn's photos (refs, oldest first, the last one now) and what Lea should know about them; nil = words only.
    func photos() async -> (refs: [String], live: [String: String])?
    /// Lea finished answering (the window closes a little later).
    func answerEnded()
    /// Lea pointed at these places in the turn's last photo.
    func marks(_ marks: [LeaMark])
    /// Lea asked for the distance between two points of the last photo: what to say (nil: cannot measure here).
    func measure(_ a: CGPoint, _ b: CGPoint) -> String?
}

/// Our ARKit scanner as Lea's eyes (Object Capture keeps its camera to itself: there Lea talks without seeing). The
/// scan is looked up at each call, so a continued or restarted scan is the one used.
@MainActor
final class ScanEyes: LeaEyes {
    private let scan: () -> ScanSession?
    private let gateway: GatewayClient

    init(scan: @escaping () -> ScanSession?, gateway: GatewayClient) {
        self.scan = scan
        self.gateway = gateway
    }

    private var current: ScanSession? {
        guard let s = scan(), s.stage != .finishing else { return nil }
        if s.sight.uploader == nil {
            let gateway = self.gateway
            s.sight.uploader = { jpeg in try? await gateway.uploadJPEG(jpeg) }
        }
        return s
    }

    func turnStarted() {
        guard let s = current else { return }
        s.sight.turnStarted(s.session.currentFrame)
    }

    func photos() async -> (refs: [String], live: [String: String])? {
        guard let s = current, let got = await s.sight.photosForTurn(s.session.currentFrame) else { return nil }
        var live = s.lidarFacts()
        if got.pointed { live["pointed"] = "yes: the magenta ring in the photo" }
        if !got.depth { live["depth"] = "no LiDAR depth in this view: nothing can be measured" }
        return (got.refs, live)
    }

    func answerEnded() { scan()?.sight.answerEnded() }

    func marks(_ marks: [LeaMark]) {
        guard let s = scan() else { return }
        s.sight.pin(marks, session: s.session)
    }

    func measure(_ a: CGPoint, _ b: CGPoint) -> String? { scan()?.sight.measure(a, b) }
}

extension ScanSession {
    /// What LiDAR says now, for a looking turn: the object's box (footprint and height, mm) and the camera's distance
    /// to its centre (cm). Lea repeats these numbers; she never works them out (conversation.py PERSONA).
    func lidarFacts() -> [String: String] {
        guard let box else { return [:] }
        var out = ["object box (LiDAR)": Self.boxWords(box.size)]
        if let m = session.currentFrame?.camera.transform {
            let cam = SIMD3(m.columns.3.x, m.columns.3.y, m.columns.3.z)
            out["camera distance (LiDAR)"] = "\(Int((simd_distance(cam, box.center) * 100).rounded())) cm"
        }
        return out
    }

    /// "208 x 410 mm footprint, 76 mm tall" (x and z across, y up), as eval/live_vision measured it.
    nonisolated static func boxWords(_ size: SIMD3<Float>) -> String {
        func mm(_ v: Float) -> Int { Int((v * 1000).rounded()) }
        return "\(mm(size.x)) x \(mm(size.z)) mm footprint, \(mm(size.y)) mm tall"
    }
}

// MARK: - Drawn over the camera

/// Lea's circles (accent), the person's pointer (magenta) and the report's findings (caution), drawn where they are on
/// the object now. A circle draws itself in like a pen stroke. One that is off screen or behind the phone becomes an
/// arrow at the edge pointing to it: move that way to see it.
struct LeaSightOverlay: View {
    let scan: ScanSession

    var body: some View {
        // Follows the AR camera while there is something to draw; paused (no redraws over the camera) when there isn't.
        TimelineView(.animation(minimumInterval: 1.0 / 30, paused: scan.sight.pins.isEmpty)) { context in
            Canvas { ctx, size in
                guard let frame = scan.session.currentFrame else { return }
                let showFindings = scan.aligned && !scan.relocalizing
                let view = frame.camera.transform.inverse
                for pin in scan.sight.pins where pin.kind != .finding || showFindings {
                    let cam = view * SIMD4(pin.world, 1)
                    let p = frame.camera.projectPoint(pin.world, orientation: .portrait, viewportSize: size)
                    let onScreen = cam.z < 0 && p.x > 24 && p.y > 24 && p.x < size.width - 24 && p.y < size.height - 24
                    let tint = Self.tint(pin.kind)
                    if onScreen {
                        let t = min(1, context.date.timeIntervalSince(pin.born) / 0.6)
                        var arc = Path()
                        arc.addArc(center: p, radius: 26, startAngle: .degrees(-90), endAngle: .degrees(-90 + 360 * t), clockwise: false)
                        ctx.stroke(arc, with: .color(Palette.keyline), style: StrokeStyle(lineWidth: Line.locked + 2, lineCap: .butt))
                        ctx.stroke(arc, with: .color(tint), style: StrokeStyle(lineWidth: Line.locked, lineCap: .butt))
                        label(pin.label, at: CGPoint(x: p.x, y: p.y + 44), tint: tint, in: &ctx)
                    } else {
                        edgeArrow(toward: p, behind: cam.z >= 0, size: size, label: pin.label, tint: tint, in: &ctx)
                    }
                }
            }
            .accessibilityHidden(true)
        }
        .overlay(alignment: .topTrailing) {
            // Privacy: photos go to Lea only inside a conversation window, and it shows.
            if scan.sight.looking {
                Label("Lea is looking", systemImage: "eye")
                    .cameraChip()
                    .padding(.top, 72).padding(.trailing, 12)
                    .transition(.opacity)
                    .accessibilityLabel("Lea is looking through the camera")
            }
        }
        .allowsHitTesting(false)
        .animation(Motion.fade, value: scan.sight.looking)
    }

    private static func tint(_ kind: LeaSight.Kind) -> Color {
        switch kind {
        case .lea: Palette.next
        case .person: Palette.chalk
        case .finding: Palette.cameraCaution
        }
    }

    private func label(_ text: String, at p: CGPoint, tint: Color, in ctx: inout GraphicsContext) {
        guard !text.isEmpty else { return }
        let t = ctx.resolve(Text(text).font(.caption.weight(.semibold)).foregroundColor(Palette.chalk))
        let s = t.measure(in: CGSize(width: 220, height: 40))
        let box = CGRect(x: p.x - s.width / 2 - 8, y: p.y - s.height / 2 - 4, width: s.width + 16, height: s.height + 8)
        ctx.fill(Path(roundedRect: box, cornerRadius: box.height / 2), with: .color(Palette.smoke))
        ctx.stroke(Path(roundedRect: box, cornerRadius: box.height / 2), with: .color(tint), lineWidth: 1)
        ctx.draw(t, at: p)
    }

    /// An arrow on the screen's edge, in the direction of a pin out of view (behind the phone: the opposite way).
    private func edgeArrow(toward p: CGPoint, behind: Bool, size: CGSize, label text: String, tint: Color,
                           in ctx: inout GraphicsContext) {
        let c = CGPoint(x: size.width / 2, y: size.height / 2)
        var dx = p.x - c.x, dy = p.y - c.y
        if behind { dx = -dx; dy = -dy }
        let len = max(1, hypot(dx, dy))
        dx /= len; dy /= len
        let inset: CGFloat = 44
        let k = min((size.width / 2 - inset) / max(abs(dx), 0.001), (size.height / 2 - inset) / max(abs(dy), 0.001))
        let tip = CGPoint(x: c.x + dx * k, y: c.y + dy * k)
        let back = CGPoint(x: tip.x - dx * 18, y: tip.y - dy * 18)
        let side = CGPoint(x: -dy * 10, y: dx * 10)
        var arrow = Path()
        arrow.move(to: tip)
        arrow.addLine(to: CGPoint(x: back.x + side.x, y: back.y + side.y))
        arrow.addLine(to: CGPoint(x: back.x - side.x, y: back.y - side.y))
        arrow.closeSubpath()
        ctx.stroke(arrow, with: .color(Palette.keyline), lineWidth: 2)
        ctx.fill(arrow, with: .color(tint))
        label(text, at: CGPoint(x: back.x - dx * 34, y: back.y - dy * 34), tint: tint, in: &ctx)
    }
}
