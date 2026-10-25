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

    /// Where the person pointed, on the object (world): projected into the next photo Lea sees, wherever the phone is
    /// by then (they point, then talk, often while moving).
    @ObservationIgnored private var pointedAt: SIMD3<Float>?
    /// The camera at the moment of the last photo: its marks are pinned from here, wherever the phone is now.
    @ObservationIgnored private var lastLook: Snapshot?

    struct Snapshot {
        let transform: simd_float4x4
        let intrinsics: simd_float3x3
        let resolution: CGSize
        /// LiDAR depth of that moment (Capture/DepthAndPoints), nil without LiDAR.
        let depth: DepthCopy?
    }

    static let longSide: CGFloat = 768

    // MARK: Lea looks

    /// The photo for this turn: the camera frame upright (portrait), the person's pointer drawn in if they pointed.
    /// Remembers the camera, so the answer's marks can be pinned. `pointed` says whether the ring is in it.
    func capture(_ frame: ARFrame) -> (jpeg: Data, pointed: Bool)? {
        var image = CIImage(cvPixelBuffer: frame.capturedImage).oriented(.right)
        let scale = Self.longSide / max(image.extent.width, image.extent.height)
        image = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        let size = image.extent.size
        guard let cg = CIContext().createCGImage(image, from: image.extent) else { return nil }
        // The ring goes where the pointed spot is in this frame; none when it has gone out of view.
        let pointed = pointedAt.flatMap {
            Self.sensorPoint(of: $0, transform: frame.camera.transform, intrinsics: frame.camera.intrinsics,
                             resolution: frame.camera.imageResolution)
        }
        pointedAt = nil
        let jpeg = UIGraphicsImageRenderer(size: size).jpegData(withCompressionQuality: 0.7) { ctx in
            UIImage(cgImage: cg).draw(in: CGRect(origin: .zero, size: size))
            if let p = pointed {
                let u = Self.portrait(p)
                let r = size.width * 0.06
                let ring = UIBezierPath(ovalIn: CGRect(x: u.x * size.width - r, y: u.y * size.height - r, width: 2 * r, height: 2 * r))
                ring.lineWidth = max(3, r * 0.18)
                UIColor.magenta.setStroke()
                ring.stroke()
            }
            _ = ctx
        }
        lastLook = Self.snapshot(frame)
        return (jpeg, pointed != nil)
    }

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

    func clear() { pins = []; pointedAt = nil; lastLook = nil }

    /// ARKit restarted tracking with another origin (a continued scan relocalised, or gave up and started fresh): pins
    /// made in the old frame would point at the wrong place, and an answer still on its way was about the old frame.
    /// The report's findings stay: they are in the saved map's frame and only drawn once the scan is aligned.
    func trackingReset() {
        pins = pins.filter { $0.kind == .finding }
        pointedAt = nil
        lastLook = nil
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

extension ScanSession {
    /// The photo for Lea's turn, uploaded as a blob, and what she should know about it. Nil while the camera has no
    /// frame (the turn goes ahead on words alone).
    func lookForLea(_ gateway: GatewayClient) async -> (ref: String, live: [String: String])? {
        guard stage != .finishing, let frame = session.currentFrame, let shot = sight.capture(frame),
              let ref = try? await gateway.uploadJPEG(shot.jpeg) else { return nil }
        return (ref, shot.pointed ? ["pointed": "yes: the magenta ring in the photo"] : [:])
    }
}

// MARK: - Drawn over the camera

/// Lea's circles (accent), the person's pointer (magenta) and the report's findings (caution), drawn where they are on
/// the object now. A circle draws itself in like a pen stroke. One that is off screen or behind the phone becomes an
/// arrow at the edge pointing to it: move that way to see it.
struct LeaSightOverlay: View {
    let scan: ScanSession

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1.0 / 30)) { context in
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
                        ctx.stroke(arc, with: .color(tint), style: StrokeStyle(lineWidth: 3.5, lineCap: .round))
                        label(pin.label, at: CGPoint(x: p.x, y: p.y + 44), tint: tint, in: &ctx)
                    } else {
                        edgeArrow(toward: p, behind: cam.z >= 0, size: size, label: pin.label, tint: tint, in: &ctx)
                    }
                }
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private static func tint(_ kind: LeaSight.Kind) -> Color {
        switch kind {
        case .lea: Palette.cameraAccent
        case .person: Color(red: 1, green: 0, blue: 1)
        case .finding: Palette.caution
        }
    }

    private func label(_ text: String, at p: CGPoint, tint: Color, in ctx: inout GraphicsContext) {
        guard !text.isEmpty else { return }
        let t = ctx.resolve(Text(text).font(.caption.weight(.semibold)).foregroundColor(.white))
        let s = t.measure(in: CGSize(width: 220, height: 40))
        let box = CGRect(x: p.x - s.width / 2 - 8, y: p.y - s.height / 2 - 4, width: s.width + 16, height: s.height + 8)
        ctx.fill(Path(roundedRect: box, cornerRadius: box.height / 2), with: .color(.black.opacity(0.6)))
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
        ctx.fill(arrow, with: .color(tint))
        label(text, at: CGPoint(x: back.x - dx * 34, y: back.y - dy * 34), tint: tint, in: &ctx)
    }
}
