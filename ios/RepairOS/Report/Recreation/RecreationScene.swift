import CoreGraphics
import CoreText
import QuartzCore
import Foundation
import SceneKit
import simd

// The SceneKit scene for a recreation: the textured object on its own patch of table, faded at the rim into the dark
// stage, soft light (a bright ambient and one gentle key light from above, so the photo texture keeps its own shading
// and the shape still reads), the camera framed on the object (not on the table patch), numbered markers on the
// surface. Platform-neutral (SceneKit + CoreGraphics): the app's RecreationView and the Mac harness that renders the
// side-by-side comparisons build the same scene.

/// A problem marker: a finding's 3D point (scan frame), its number (nil = small dot) and colour.
struct RecreationMarker: Sendable, Equatable, Identifiable {
    var id: String
    var number: Int?
    var point: SIMD3<Float>
    /// sRGB 0...1.
    var rgb: SIMD3<Float>
    /// The problem area's radius on the scan (metres): drawn as a glowing ring round the point; nil draws none.
    var ring: Float? = nil
}

/// What the stage draws.
enum RecreationContent: @unchecked Sendable {
    case texturedMesh(TexturedMesh, CGImage)
    /// A scene file (USDZ) loaded by SceneKit.
    case sceneFile(SCNScene)
    /// Coloured points (the phone's LiDAR cloud): only as a fallback and for comparisons.
    case points([SIMD3<Float>], [SIMD3<Float>]?)
}

@MainActor
final class RecreationStage {
    let scene = SCNScene()
    let cameraNode = SCNNode()
    /// Turns about the focus centre (the slow turntable); `content` sits inside it.
    let pivot = SCNNode()
    let content = SCNNode()
    let modelNode = SCNNode()
    let markerRoot = SCNNode()
    private(set) var focusCenter = SIMD3<Float>(0, 0, 0)
    private(set) var focusRadius: Float = 0.1
    private(set) var markers: [RecreationMarker] = []
    private var spinning = false
    /// The drawn surface's points (scan frame), for putting markers on the surface.
    private var surface: [SIMD3<Float>] = []
    /// A marker further than this from the drawn surface keeps its own point (it is not on this object).
    static let snapLimit: Float = 0.05

    static let fieldOfView: CGFloat = 30
    static let elevation: Float = 0.42      // radians above the horizon (about 24°)
    static let spinSeconds: TimeInterval = 36
    /// Small objects are not framed closer than this radius: the LiDAR surface of a 3 cm object is a blob up close.
    static let minRadius: Float = 0.045

    init() {
        let camera = SCNCamera()
        camera.fieldOfView = Self.fieldOfView
        camera.zNear = 0.005
        camera.zFar = 20
        camera.wantsHDR = false
        cameraNode.camera = camera
        scene.rootNode.addChildNode(cameraNode)
        content.addChildNode(modelNode)
        content.addChildNode(markerRoot)
        pivot.addChildNode(content)
        scene.rootNode.addChildNode(pivot)
        addLights()
    }

    // MARK: Content

    /// Puts `content` on the stage. `focus` is the object's centre and radius (the locked box); nil frames the
    /// model's bounds.
    func set(_ c: RecreationContent, focus: (center: SIMD3<Float>, radius: Float)?) {
        modelNode.childNodes.forEach { $0.removeFromParentNode() }
        modelNode.geometry = nil
        var lo = SIMD3<Float>(repeating: .infinity), hi = -lo
        switch c {
        case .texturedMesh(let mesh, let image):
            modelNode.geometry = Self.geometry(mesh, texture: image)
            surface = mesh.positions
            (lo, hi) = mesh.bounds
        case .sceneFile(let s):
            for child in s.rootNode.childNodes { modelNode.addChildNode(child) }
            surface = Self.points(of: modelNode)
            let (a, b) = modelNode.boundingBox
            lo = SIMD3(a); hi = SIMD3(b)
            modelNode.enumerateHierarchy { n, _ in n.geometry?.materials.forEach { Self.soften($0) } }
        case .points(let p, let colors):
            surface = p
            modelNode.geometry = Self.pointGeometry(p, colors: colors)
            for q in p { lo = simd_min(lo, q); hi = simd_max(hi, q) }
        }
        if let focus {
            focusCenter = focus.center
            focusRadius = max(0.02, focus.radius)
        } else if lo.x.isFinite {
            focusCenter = (lo + hi) / 2
            focusRadius = max(0.02, simd_length(hi - lo) / 2)
        }
        // The table patch fades out around the object: fully there within 1.3 focus radii of the centre (horizontally),
        // gone by 2.2 radii. Measured in the model's own frame (the scan frame), so the turntable does not move it.
        let fade = Self.fadeModifier
        modelNode.enumerateHierarchy { n, _ in
            n.geometry?.materials.forEach { m in
                m.shaderModifiers = [.fragment: fade]
                m.setValue(NSValue(scnVector3: SCNVector3(focusCenter)), forKey: "fadeCenter")
                m.setValue(NSNumber(value: focusRadius * 1.3), forKey: "fadeInner")
                m.setValue(NSNumber(value: focusRadius * 2.2), forKey: "fadeOuter")
            }
        }
        pivot.simdPosition = focusCenter
        content.simdPosition = -focusCenter
        frame(animated: false)
    }

    // MARK: Camera

    /// The camera distance at which the object fills the view's shorter side (its bounding sphere slightly cropped: an
    /// object rarely fills its sphere).
    var frameDistance: Float {
        max(focusRadius, Self.minRadius) / Float(sin(Self.fieldOfView * .pi / 360))
    }

    func frame(animated: Bool, azimuth: Float = 0.6) {
        let d = frameDistance
        let dir = SIMD3<Float>(sin(azimuth) * cos(Self.elevation), sin(Self.elevation), cos(azimuth) * cos(Self.elevation))
        move(to: focusCenter + dir * d, look: focusCenter, animated: animated)
    }

    func move(to position: SIMD3<Float>, look target: SIMD3<Float>, animated: Bool) {
        SCNTransaction.begin()
        SCNTransaction.animationDuration = animated ? 0.7 : 0
        SCNTransaction.animationTimingFunction = CAMediaTimingFunction(controlPoints: 0.32, 0.72, 0, 1)
        cameraNode.simdWorldPosition = position
        cameraNode.simdLook(at: target, up: SIMD3(0, 1, 0), localFront: SIMD3(0, 0, -1))
        SCNTransaction.commit()
    }

    /// Flies to a marker from the side it faces (away from the object's centre, a little from above).
    func fly(to id: String, animated: Bool) {
        guard let node = markerRoot.childNodes.first(where: { $0.name == "marker:\(id)" }) else { return }
        let target = node.presentation.simdWorldPosition
        var dir = target - focusCenter
        dir.y = max(dir.y, 0) + 0.6 * simd_length(SIMD2(dir.x, dir.z))
        let from = simd_length(dir) > 1e-4 ? simd_normalize(dir) : SIMD3<Float>(0, 0.5, 0.87)
        move(to: target + from * frameDistance * 0.55, look: target, animated: animated)
    }

    // MARK: Spin

    func startSpin() {
        guard !spinning else { return }
        spinning = true
        pivot.runAction(.repeatForever(.rotateBy(x: 0, y: -2 * .pi, z: 0, duration: Self.spinSeconds)), forKey: "spin")
    }

    func stopSpin() {
        guard spinning else { return }
        spinning = false
        pivot.removeAction(forKey: "spin")
    }

    var isSpinning: Bool { spinning }

    // MARK: Markers

    func place(_ markers: [RecreationMarker], focus: String?) {
        self.markers = markers
        markerRoot.childNodes.forEach { $0.removeFromParentNode() }
        let ranked = markers.sorted { ($0.number ?? 99) < ($1.number ?? 99) }
        for m in ranked {
            let side = CGFloat(focusRadius) * (m.number == nil ? 0.07 : 0.17)
            let plane = SCNPlane(width: side, height: side)
            let mat = SCNMaterial()
            mat.diffuse.contents = Self.badge(m)
            mat.lightingModel = .constant
            mat.isDoubleSided = true
            mat.readsFromDepthBuffer = false
            mat.writesToDepthBuffer = false
            plane.materials = [mat]
            let node = SCNNode(geometry: plane)
            node.name = "marker:\(m.id)"
            let at = onSurface(m.point)
            node.simdPosition = at
            node.renderingOrder = m.number == nil ? 10 : 30 - (m.number ?? 0)
            node.constraints = [SCNBillboardConstraint()]
            let on = focus == m.id
            node.simdScale = SIMD3(repeating: on ? 1.3 : 1)
            node.opacity = focus == nil || on ? (m.number == nil ? 0.8 : 1) : 0.45
            if let r = ringRadius(m) {
                // The problem area circled on the object (it stays on the spot); the badge, still its own node for the
                // separator, is drawn at the ring's upper right (a pivot in its billboard plane) so the spot stays visible.
                let ring = ringNode(m, radius: r, on: on)
                ring.simdPosition = at
                ring.opacity = focus == nil || on ? 1 : 0.4
                markerRoot.addChildNode(ring)
                var pivot = matrix_identity_float4x4
                pivot.columns.3 = SIMD4(-r * 0.72, -r * 0.72, 0, 1)
                node.simdPivot = pivot
            }
            // A small dot on the surface itself, hidden where the object is in front of it.
            let dot = SCNSphere(radius: CGFloat(focusRadius) * 0.012)
            let dm = SCNMaterial()
            dm.diffuse.contents = CGColor(srgbRed: CGFloat(m.rgb.x), green: CGFloat(m.rgb.y), blue: CGFloat(m.rgb.z), alpha: 1)
            dm.lightingModel = .constant
            dot.materials = [dm]
            let dotNode = SCNNode(geometry: dot)
            dotNode.simdPosition = at
            markerRoot.addChildNode(dotNode)
            markerRoot.addChildNode(node)
        }
    }

    /// The ring's radius: the problem area's size, at least a fingertip on screen and never most of the object.
    func ringRadius(_ m: RecreationMarker) -> Float? {
        guard let r = m.ring, r > 0 else { return nil }
        return min(max(r, focusRadius * 0.06, 0.006), focusRadius * 0.45, 0.06)
    }

    /// A billboard circle round the problem area: a soft glow and a stroke in the marker's colour (dashed for a spot the
    /// person circled), a little larger while it is the selected one. A tap on it selects its marker (`markerID`).
    private func ringNode(_ m: RecreationMarker, radius r: Float, on: Bool) -> SCNNode {
        let side = CGFloat(r) * 2 * 1.3
        let plane = SCNPlane(width: side, height: side)
        let mat = SCNMaterial()
        mat.diffuse.contents = Self.ring(rgb: m.rgb, dashed: m.number == nil)
        mat.lightingModel = .constant
        mat.isDoubleSided = true
        mat.readsFromDepthBuffer = false
        mat.writesToDepthBuffer = false
        plane.materials = [mat]
        let node = SCNNode(geometry: plane)
        node.name = "ring:\(m.id)"
        node.renderingOrder = 5
        node.constraints = [SCNBillboardConstraint()]
        // Static, not an animation: the view draws on demand once the turntable stops, which would freeze it.
        node.simdScale = SIMD3(repeating: on ? 1.12 : 1)
        return node
    }

    /// A finding's point moved onto the drawn surface: the findings are located on the phone's raw LiDAR points,
    /// which sit a few millimetres to centimetres off the fused surface (measured on five real scans: median 14 mm,
    /// max 39 mm), so an unsnapped marker floats beside the object. Points further than `snapLimit` stay put.
    func onSurface(_ p: SIMD3<Float>) -> SIMD3<Float> {
        var best = p, bestD = Self.snapLimit * Self.snapLimit
        for q in surface {
            let d = simd_distance_squared(p, q)
            if d < bestD { bestD = d; best = q }
        }
        return best
    }

    /// Every vertex of the geometry under `node`, in the node's own frame (the scan frame for a recreation file).
    static func points(of node: SCNNode) -> [SIMD3<Float>] {
        var out: [SIMD3<Float>] = []
        node.enumerateHierarchy { n, _ in
            guard let g = n.geometry, let src = g.sources(for: .vertex).first, src.usesFloatComponents,
                  src.bytesPerComponent == 4, src.componentsPerVector >= 3 else { return }
            let m = n.simdConvertTransform(matrix_identity_float4x4, to: node)
            src.data.withUnsafeBytes { raw in
                for i in 0..<src.vectorCount {
                    let o = src.dataOffset + i * src.dataStride
                    let v = SIMD3(raw.loadUnaligned(fromByteOffset: o, as: Float.self),
                                  raw.loadUnaligned(fromByteOffset: o + 4, as: Float.self),
                                  raw.loadUnaligned(fromByteOffset: o + 8, as: Float.self))
                    let w = m * SIMD4(v, 1)
                    out.append(SIMD3(w.x, w.y, w.z))
                }
            }
        }
        return out
    }

    func markerID(atHit nodes: [SCNNode]) -> String? {
        markerID(atHit: nodes.map { ($0, nil) })
    }

    /// The marker a tap selects: a number badge first; else a ring, but only inside its circle (the ring is drawn on a
    /// square plane whose clear corners must not take a tap meant for a nearby badge; Greptile, PR #25). `local` is the
    /// hit's point in the node's own plane; nil (no position known) counts as inside.
    func markerID(atHit hits: [(node: SCNNode, local: SIMD3<Float>?)]) -> String? {
        if let badge = hits.lazy.compactMap({ $0.node.name }).first(where: { $0.hasPrefix("marker:") }) {
            return String(badge.dropFirst("marker:".count))
        }
        for hit in hits {
            guard let name = hit.node.name, name.hasPrefix("ring:"), let plane = hit.node.geometry as? SCNPlane else { continue }
            let radius = Float(plane.width) / 2 / 1.3 * 1.15   // the circle (1/1.3 of the plane) and its stroke
            if let p = hit.local, simd_length(SIMD2(p.x, p.y)) > radius { continue }
            return String(name.dropFirst("ring:".count))
        }
        return nil
    }

    // MARK: Building blocks

    private func addLights() {
        let ambient = SCNLight()
        ambient.type = .ambient
        ambient.intensity = 780
        ambient.color = CGColor(gray: 1, alpha: 1)
        let a = SCNNode()
        a.light = ambient
        scene.rootNode.addChildNode(a)
        // Key: from above and in front of the camera's start, attached to the camera so the lit side follows the
        // viewer (the turntable turns the object under it).
        let key = SCNLight()
        key.type = .directional
        key.intensity = 360
        key.color = CGColor(gray: 1, alpha: 1)
        let k = SCNNode()
        k.light = key
        k.simdEulerAngles = SIMD3(-0.55, -0.35, 0)
        cameraNode.addChildNode(k)
    }

    /// Fragment shader modifier: fades the table patch around the object (horizontal distance from the focus centre in
    /// the model's frame, which is the scan frame) into the stage.
    static let fadeModifier = """
    #pragma arguments
    float3 fadeCenter;
    float fadeInner;
    float fadeOuter;
    #pragma transparent
    #pragma body
    float4 mp = scn_node.inverseModelViewTransform * float4(_surface.position, 1.0);
    float r = length(mp.xz - fadeCenter.xz);
    float a = 1.0 - smoothstep(fadeInner, fadeOuter, r);
    _output.color *= a;
    """

    /// Photo-textured materials: the texture is the colour, light only shades it gently.
    static func soften(_ m: SCNMaterial) {
        m.lightingModel = .lambert
        m.isDoubleSided = true
        m.diffuse.mipFilter = .linear
        m.diffuse.minificationFilter = .linear
        m.diffuse.magnificationFilter = .linear
        m.diffuse.wrapS = .clamp
        m.diffuse.wrapT = .clamp
        m.specular.contents = CGColor(gray: 0, alpha: 1)
    }

    static func geometry(_ mesh: TexturedMesh, texture: CGImage) -> SCNGeometry {
        let n = mesh.positions.count
        let normals = mesh.normals()
        func src3(_ v: [SIMD3<Float>], _ s: SCNGeometrySource.Semantic) -> SCNGeometrySource {
            SCNGeometrySource(data: v.withUnsafeBufferPointer { Data(buffer: $0) }, semantic: s, vectorCount: n,
                              usesFloatComponents: true, componentsPerVector: 3, bytesPerComponent: 4, dataOffset: 0,
                              dataStride: MemoryLayout<SIMD3<Float>>.stride)
        }
        let uv = SCNGeometrySource(data: mesh.uvs.withUnsafeBufferPointer { Data(buffer: $0) }, semantic: .texcoord,
                                   vectorCount: n, usesFloatComponents: true, componentsPerVector: 2, bytesPerComponent: 4,
                                   dataOffset: 0, dataStride: MemoryLayout<SIMD2<Float>>.stride)
        let element = SCNGeometryElement(data: mesh.indices.withUnsafeBufferPointer { Data(buffer: $0) },
                                         primitiveType: .triangles, primitiveCount: mesh.triangleCount, bytesPerIndex: 4)
        let g = SCNGeometry(sources: [src3(mesh.positions, .vertex), src3(normals, .normal), uv], elements: [element])
        let m = SCNMaterial()
        m.diffuse.contents = texture
        soften(m)
        g.materials = [m]
        return g
    }

    static func pointGeometry(_ p: [SIMD3<Float>], colors: [SIMD3<Float>]?) -> SCNGeometry {
        let n = p.count
        var sources = [SCNGeometrySource(data: p.withUnsafeBufferPointer { Data(buffer: $0) }, semantic: .vertex,
                                         vectorCount: n, usesFloatComponents: true, componentsPerVector: 3,
                                         bytesPerComponent: 4, dataOffset: 0, dataStride: MemoryLayout<SIMD3<Float>>.stride)]
        if let colors, colors.count == n {
            sources.append(SCNGeometrySource(data: colors.withUnsafeBufferPointer { Data(buffer: $0) }, semantic: .color,
                                             vectorCount: n, usesFloatComponents: true, componentsPerVector: 3,
                                             bytesPerComponent: 4, dataOffset: 0, dataStride: MemoryLayout<SIMD3<Float>>.stride))
        }
        let idx = Array(0..<UInt32(n))
        let e = SCNGeometryElement(data: idx.withUnsafeBufferPointer { Data(buffer: $0) }, primitiveType: .point,
                                   primitiveCount: n, bytesPerIndex: 4)
        e.pointSize = 3
        e.minimumPointScreenSpaceRadius = 1.2
        e.maximumPointScreenSpaceRadius = 4.5
        let g = SCNGeometry(sources: sources, elements: [e])
        let m = SCNMaterial()
        m.lightingModel = .constant
        m.diffuse.contents = CGColor(gray: 1, alpha: 1)
        g.materials = [m]
        return g
    }

    /// The ring texture: the circle at 1/1.3 of the image (room for the glow), drawn once per colour and style.
    static func ring(rgb: SIMD3<Float>, dashed: Bool) -> CGImage? {
        let side = 256, c = CGFloat(side) / 2, r = CGFloat(side) / 2 / 1.3
        guard let ctx = CGContext(data: nil, width: side, height: side, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        let colour = CGColor(srgbRed: CGFloat(rgb.x), green: CGFloat(rgb.y), blue: CGFloat(rgb.z), alpha: 1)
        let circle = CGRect(x: c - r, y: c - r, width: 2 * r, height: 2 * r)
        ctx.setShadow(offset: .zero, blur: CGFloat(side) * 0.06, color: colour)
        ctx.setStrokeColor(colour)
        ctx.setLineWidth(CGFloat(side) * 0.035)
        if dashed { ctx.setLineDash(phase: 0, lengths: [CGFloat(side) * 0.07, CGFloat(side) * 0.045]) }
        ctx.strokeEllipse(in: circle)
        ctx.setShadow(offset: .zero, blur: 0, color: nil)
        ctx.setStrokeColor(CGColor(gray: 1, alpha: 0.85))
        ctx.setLineWidth(CGFloat(side) * 0.008)
        ctx.strokeEllipse(in: circle)
        return ctx.makeImage()
    }

    /// A round badge: white ring, marker colour, the number in white (CoreText, so it draws the same everywhere).
    static func badge(_ m: RecreationMarker) -> CGImage? {
        let side = m.number == nil ? 48 : 96
        guard let ctx = CGContext(data: nil, width: side, height: side, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        let rect = CGRect(x: 0, y: 0, width: side, height: side)
        ctx.setFillColor(CGColor(gray: 1, alpha: 1))
        ctx.fillEllipse(in: rect)
        ctx.setFillColor(CGColor(srgbRed: CGFloat(m.rgb.x), green: CGFloat(m.rgb.y), blue: CGFloat(m.rgb.z), alpha: 1))
        ctx.fillEllipse(in: rect.insetBy(dx: CGFloat(side) * 0.08, dy: CGFloat(side) * 0.08))
        if let number = m.number {
            let font = CTFontCreateWithName("SFProRounded-Bold" as CFString, CGFloat(side) * 0.5, nil)
            let attrs: [NSAttributedString.Key: Any] = [
                NSAttributedString.Key(kCTFontAttributeName as String): font,
                NSAttributedString.Key(kCTForegroundColorAttributeName as String): CGColor(gray: 1, alpha: 1)]
            let line = CTLineCreateWithAttributedString(NSAttributedString(string: "\(number)", attributes: attrs))
            let b = CTLineGetImageBounds(line, ctx)
            ctx.textPosition = CGPoint(x: (CGFloat(side) - b.width) / 2 - b.minX, y: (CGFloat(side) - b.height) / 2 - b.minY)
            CTLineDraw(line, ctx)
        }
        return ctx.makeImage()
    }
}

extension RecreationMarker {
    /// The report's tone colours (Design/Theme.swift `Tone`), as plain sRGB for SceneKit.
    static func rgb(tone: String) -> SIMD3<Float> {
        switch tone {
        case "danger": SIMD3(0.92, 0.26, 0.24)
        case "caution": SIMD3(0.96, 0.62, 0.12)
        case "good": SIMD3(0.20, 0.74, 0.42)
        case "cosmetic": SIMD3(0.45, 0.55, 0.95)
        case "setup": SIMD3(0.36, 0.79, 0.83)
        default: SIMD3(0.55, 0.55, 0.62)
        }
    }
}
