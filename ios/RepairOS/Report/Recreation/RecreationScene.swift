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
    /// The problem area's radius on the scan (metres): drawn as a ring on the surface round the point; nil draws none.
    var ring: Float? = nil
    /// The part's name while Lea shows it: a billboard label at the end of a short leader out along the surface normal.
    var label: String? = nil
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
    /// The drawn surface's points (scan frame), for putting markers on the surface, and their normals (textured
    /// meshes only) for laying rings on it.
    private var surface: [SIMD3<Float>] = []
    private var surfaceNormals: [SIMD3<Float>] = []
    /// Rings leaving the stage fade here, so a new move never pops the old ring away.
    let fadingRoot = SCNNode()
    /// Lea's surface highlight: the active spot and the one fading out (shader uniforms on the model's materials).
    private var highlight: (center: SIMD3<Float>, radius: Float)?
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
        content.addChildNode(fadingRoot)
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
            surfaceNormals = mesh.normals()
            (lo, hi) = mesh.bounds
        case .sceneFile(let s):
            for child in s.rootNode.childNodes { modelNode.addChildNode(child) }
            surface = Self.points(of: modelNode)
            surfaceNormals = []
            let (a, b) = modelNode.boundingBox
            lo = SIMD3(a); hi = SIMD3(b)
            modelNode.enumerateHierarchy { n, _ in n.geometry?.materials.forEach { Self.soften($0) } }
        case .points(let p, let colors):
            surface = p
            surfaceNormals = []
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
                Self.resetHighlight(m)
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
        let p = framePose(azimuth: azimuth)
        move(to: p.eye, look: p.look, animated: animated)
    }

    /// The whole object, from `azimuth` and a little above.
    func framePose(azimuth: Float = 0.6) -> TourPose {
        let dir = SIMD3<Float>(sin(azimuth) * cos(Self.elevation), sin(Self.elevation), cos(azimuth) * cos(Self.elevation))
        return TourPose(eye: focusCenter + dir * frameDistance, look: focusCenter)
    }

    func move(to position: SIMD3<Float>, look target: SIMD3<Float>, animated: Bool) {
        SCNTransaction.begin()
        SCNTransaction.animationDuration = animated ? Motion.Scene.fly : 0
        SCNTransaction.animationTimingFunction = Motion.Scene.curve
        cameraNode.simdWorldPosition = position
        cameraNode.simdLook(at: target, up: SIMD3(0, 1, 0), localFront: SIMD3(0, 0, -1))
        SCNTransaction.commit()
    }

    /// Flies to a marker from the side it faces (away from the object's centre, a little from above).
    func fly(to id: String, animated: Bool) {
        guard let p = markerPose(id) else { return }
        move(to: p.eye, look: p.look, animated: animated)
    }

    /// The view of one marker `fly(to:)` lands on; nil when there is no such marker.
    func markerPose(_ id: String) -> TourPose? {
        guard let node = markerRoot.childNodes.first(where: { $0.name == "marker:\(id)" }) else { return nil }
        // The model tree, not the presentation: a marker placed this frame (one of Lea's) has no presentation yet, and
        // its presentation position would ignore the turntable's turn.
        let target = node.simdWorldPosition
        var dir = target - focusCenter
        dir.y = max(dir.y, 0) + 0.6 * simd_length(SIMD2(dir.x, dir.z))
        let from = simd_length(dir) > 1e-4 ? simd_normalize(dir) : SIMD3<Float>(0, 0.5, 0.87)
        return TourPose(eye: target + from * frameDistance * 0.55, look: target)
    }

    /// The walkthrough's view of a step: its marker, or the whole object.
    func tourPose(_ id: String?) -> TourPose { id.flatMap(markerPose) ?? framePose() }

    /// Lights the markers by weight (TourCamera.weights) without rebuilding them: a marker at 1 is the selected one
    /// (full, a little larger, its ring bright), the rest dim as it lights up; all at 0 is the resting look.
    func emphasize(_ weights: [String: Float]) {
        let lit = weights.values.max() ?? 0
        for node in markerRoot.childNodes {
            guard let name = node.name else { continue }
            let ring = name.hasPrefix("ring:")
            guard ring || name.hasPrefix("marker:"), let id = name.split(separator: ":", maxSplits: 1).last.map(String.init),
                  let m = markers.first(where: { $0.id == id }) else { continue }
            let on = weights[id] ?? 0
            let shown = max(on, 1 - lit)
            if ring {
                node.opacity = CGFloat(0.4 + 0.6 * shown)
                node.simdScale = SIMD3(repeating: 1 + 0.12 * on)
            } else {
                node.opacity = CGFloat((m.number == nil ? 0.8 : 1) * (0.45 + 0.55 * shown))
                node.simdScale = SIMD3(repeating: 1 + 0.3 * on)
            }
        }
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
        // Rings that leave (Lea moved on) fade out instead of popping away.
        let keep = Set(markers.filter { $0.ring != nil }.map { "ring:\($0.id)" })
        for node in markerRoot.childNodes where (node.name?.hasPrefix("ring:") ?? false) && !keep.contains(node.name ?? "") {
            node.removeFromParentNode()
            node.name = nil
            fadingRoot.addChildNode(node)
            node.runAction(.sequence([.fadeOut(duration: 0.25), .removeFromParentNode()]))
        }
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
            let frame = surfaceFrame(at: at, radius: ringRadius(m) ?? focusRadius * 0.05)
            node.simdScale = SIMD3(repeating: on ? 1.3 : 1)
            node.opacity = focus == nil || on ? (m.number == nil ? 0.8 : 1) : 0.45
            if let r = ringRadius(m) {
                // The problem area circled on the object (it stays on the spot); the badge, still its own node for the
                // separator, is drawn at the ring's upper right (a pivot in its billboard plane) so the spot stays visible.
                let ring = ringNode(m, radius: r, on: on, normal: frame)
                ring.simdPosition = frame.map { at + $0 * 0.001 } ?? at   // 1 mm off the surface, along its normal
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
            if let text = m.label?.trimmingCharacters(in: .whitespaces), !text.isEmpty {
                addLabel(text, for: m, at: at, normal: frame ?? SIMD3(0, 1, 0))
            }
        }
    }

    /// The local surface at `p`: the average normal of the mesh within `radius`, or nil where it is too noisy to lay
    /// a ring on (normals that disagree) or there are no normals (a scene file, points).
    func surfaceFrame(at p: SIMD3<Float>, radius: Float) -> SIMD3<Float>? {
        guard surfaceNormals.count == surface.count, !surface.isEmpty else { return nil }
        let r2 = max(radius, 0.004) * max(radius, 0.004)
        var sum = SIMD3<Float>(repeating: 0)
        var n = 0
        for i in surface.indices where simd_distance_squared(surface[i], p) < r2 {
            sum += surfaceNormals[i]
            n += 1
        }
        guard n >= 3 else { return nil }
        let mean = sum / Float(n)
        // Coherence: unit normals that agree average to nearly unit length; a noisy patch averages short.
        guard simd_length(mean) > 0.6 else { return nil }
        return simd_normalize(mean)
    }

    /// The camera's position in the scan frame (the turntable turns the model under it).
    private var cameraInScan: SIMD3<Float> { content.simdConvertPosition(cameraNode.presentation.simdWorldPosition, from: nil) }

    /// The 3D label: a 1 pt leader from the surface out along the normal to a billboard label (legible from any
    /// side). Hidden until Lea's move lands (`arrive`).
    private func addLabel(_ text: String, for m: RecreationMarker, at p: SIMD3<Float>, normal: SIMD3<Float>) {
        let length = max(focusRadius * 0.28, 0.02)
        let end = p + normal * length
        let leader = SCNNode(geometry: Self.lineGeometry(from: p, to: end))
        leader.name = "leader:\(m.id)"
        leader.opacity = 0
        leader.renderingOrder = 40
        markerRoot.addChildNode(leader)
        let image = PartLabel.image(text)
        let height = CGFloat(focusRadius) * 0.11
        let width = height * image.size.width / max(1, image.size.height)
        let plane = SCNPlane(width: width, height: height)
        let mat = SCNMaterial()
        mat.diffuse.contents = image
        mat.lightingModel = .constant
        mat.isDoubleSided = true
        mat.readsFromDepthBuffer = false
        mat.writesToDepthBuffer = false
        plane.materials = [mat]
        let label = SCNNode(geometry: plane)
        label.name = "label:\(m.id)"
        label.simdPosition = end
        // Anchored at its lower-left corner at the leader's end, a billboard.
        var pivot = matrix_identity_float4x4
        pivot.columns.3 = SIMD4(-Float(width) / 2 + Float(height) * 0.2, -Float(height) / 2, 0, 1)
        label.simdPivot = pivot
        label.constraints = [SCNBillboardConstraint()]
        label.opacity = 0
        label.renderingOrder = 41
        markerRoot.addChildNode(label)
    }

    /// Lea's move landed on `id` (the camera about 80% settled): the ring draws on (trim 0 → 1, 0.35 s), the part
    /// lights up on the surface (radius 0 → r), then the label slides 5 pt and fades in. Reduce Motion: all at once.
    func arrive(_ id: String, animated: Bool) {
        guard let m = markers.first(where: { $0.id == id }) else { return }
        let at = onSurface(m.point)
        let r = ringRadius(m) ?? max(focusRadius * 0.06, 0.008)
        if let ring = markerRoot.childNodes.first(where: { $0.name == "ring:\(id)" }), let mat = ring.geometry?.firstMaterial {
            if animated {
                mat.setValue(NSNumber(value: 0), forKey: "drawn")
                let span = Float(Motion.Scene.drawOn)
                ring.runAction(.customAction(duration: Motion.Scene.drawOn) { node, t in
                    node.geometry?.firstMaterial?.setValue(NSNumber(value: Float(t) / span), forKey: "drawn")
                })
            } else {
                mat.setValue(NSNumber(value: 1), forKey: "drawn")
            }
        }
        light(at, radius: r * 1.15, animated: animated)
        for prefix in ["leader:", "label:"] {
            guard let node = markerRoot.childNodes.first(where: { $0.name == "\(prefix)\(id)" }) else { continue }
            if animated {
                let rest = node.simdPosition
                let slide = simd_normalize(cameraNode.presentation.simdWorldPosition - node.simdWorldPosition)
                node.simdPosition = rest - SIMD3(slide.y, -slide.x, 0) * focusRadius * 0.02
                node.runAction(.sequence([.wait(duration: Motion.Scene.drawOn * 0.6),
                                          .group([.fadeIn(duration: 0.2), .move(to: SCNVector3(rest), duration: 0.25)])]))
            } else {
                node.opacity = 1
            }
        }
    }

    /// Hides the hidden-until-arrival pieces for `id` again before its move starts (the ring undrawn).
    func prepareArrival(_ id: String) {
        if let mat = markerRoot.childNodes.first(where: { $0.name == "ring:\(id)" })?.geometry?.firstMaterial {
            mat.setValue(NSNumber(value: 0), forKey: "drawn")
        }
    }

    /// The surface highlight: the part itself lights up (a soft violet rim and a gentle lift, falling off), its radius
    /// growing in over 0.35 s; the previous one fades out (one active, one fading). nil clears it (fades).
    func light(_ center: SIMD3<Float>?, radius: Float = 0, animated: Bool) {
        let materials = Self.materials(of: modelNode)
        // The old spot moves to the fading slot.
        if let old = highlight {
            for m in materials {
                m.setValue(NSValue(scnVector3: SCNVector3(old.center)), forKey: "hl2Center")
                m.setValue(NSNumber(value: old.radius), forKey: "hl2Radius")
                m.setValue(NSNumber(value: 1), forKey: "hl2Amount")
            }
            modelNode.removeAction(forKey: "hl2")
            if animated {
                modelNode.runAction(.customAction(duration: 0.25) { node, t in
                    Self.setAll(node, 1 - Float(t) / 0.25, "hl2Amount")
                }, forKey: "hl2")
            } else {
                for m in materials { m.setValue(NSNumber(value: 0), forKey: "hl2Amount") }
            }
        }
        highlight = center.map { ($0, radius) }
        modelNode.removeAction(forKey: "hl")
        guard let center else {
            for m in materials { m.setValue(NSNumber(value: 0), forKey: "hlAmount") }
            return
        }
        for m in materials {
            m.setValue(NSValue(scnVector3: SCNVector3(center)), forKey: "hlCenter")
            m.setValue(NSNumber(value: 1), forKey: "hlAmount")
            m.setValue(NSNumber(value: animated ? 0 : radius), forKey: "hlRadius")
        }
        guard animated else { return }
        let span = Float(Motion.Scene.drawOn)
        modelNode.runAction(.customAction(duration: Motion.Scene.drawOn) { node, t in
            let k = Float(t) / span
            Self.setAll(node, radius * (1 - (1 - k) * (1 - k) * (1 - k)), "hlRadius")
        }, forKey: "hl")
    }

    /// Sets one shader uniform on every material under `node` (from an SCNAction, on SceneKit's render thread).
    nonisolated private static func setAll(_ node: SCNNode, _ value: Float, _ key: String) {
        node.enumerateHierarchy { n, _ in n.geometry?.materials.forEach { $0.setValue(NSNumber(value: value), forKey: key) } }
    }

    private static func materials(of node: SCNNode) -> [SCNMaterial] {
        var out: [SCNMaterial] = []
        node.enumerateHierarchy { n, _ in out += n.geometry?.materials ?? [] }
        return out
    }

    private static func resetHighlight(_ m: SCNMaterial) {
        m.setValue(NSValue(scnVector3: SCNVector3Zero), forKey: "hlCenter")
        m.setValue(NSNumber(value: 0), forKey: "hlRadius")
        m.setValue(NSNumber(value: 0), forKey: "hlAmount")
        m.setValue(NSValue(scnVector3: SCNVector3Zero), forKey: "hl2Center")
        m.setValue(NSNumber(value: 0), forKey: "hl2Radius")
        m.setValue(NSNumber(value: 0), forKey: "hl2Amount")
        let v = StageMarkImages.rgb(.accent)
        m.setValue(NSValue(scnVector3: SCNVector3(v)), forKey: "hlColor")
    }

    /// A thin line (two vertices) in chalk, drawn over the model.
    static func lineGeometry(from a: SIMD3<Float>, to b: SIMD3<Float>) -> SCNGeometry {
        let v = [a, b]
        let src = SCNGeometrySource(data: v.withUnsafeBufferPointer { Data(buffer: $0) }, semantic: .vertex, vectorCount: 2,
                                    usesFloatComponents: true, componentsPerVector: 3, bytesPerComponent: 4, dataOffset: 0,
                                    dataStride: MemoryLayout<SIMD3<Float>>.stride)
        let idx: [UInt16] = [0, 1]
        let e = SCNGeometryElement(data: idx.withUnsafeBufferPointer { Data(buffer: $0) }, primitiveType: .line,
                                   primitiveCount: 1, bytesPerIndex: 2)
        let g = SCNGeometry(sources: [src], elements: [e])
        let m = SCNMaterial()
        m.diffuse.contents = CGColor(srgbRed: 0xF4 / 255, green: 0xF2 / 255, blue: 0xEC / 255, alpha: 1)
        m.lightingModel = .constant
        m.readsFromDepthBuffer = false
        m.writesToDepthBuffer = false
        g.materials = [m]
        return g
    }

    /// The ring's radius: the problem area's size, at least a fingertip on screen and never most of the object.
    func ringRadius(_ m: RecreationMarker) -> Float? {
        guard let r = m.ring, r > 0 else { return nil }
        return min(max(r, focusRadius * 0.06, 0.006), focusRadius * 0.45, 0.06)
    }

    /// A billboard circle round the problem area: the marker's colour over a keyline (dashed only for a spot the person
    /// circled and no scan has checked yet: unknown), a little larger while it is the selected one. A tap on it selects its marker (`markerID`).
    private func ringNode(_ m: RecreationMarker, radius r: Float, on: Bool, normal: SIMD3<Float>?) -> SCNNode {
        let side = CGFloat(r) * 2 * 1.3
        let plane = SCNPlane(width: side, height: side)
        let mat = SCNMaterial()
        mat.diffuse.contents = Self.ring(rgb: m.rgb, dashed: m.id.hasPrefix("target:"))
        mat.lightingModel = .constant
        mat.isDoubleSided = true
        mat.shaderModifiers = [.fragment: Self.drawOnModifier]
        mat.setValue(NSNumber(value: 1), forKey: "drawn")
        plane.materials = [mat]
        let node = SCNNode(geometry: plane)
        node.name = "ring:\(m.id)"
        node.renderingOrder = 5
        // On the surface (oriented by its normal, depth-tested so it wraps behind edges), unless the surface there is
        // too noisy or faces more than ~70° away from the camera: then the flat billboard, always visible.
        let toCamera = simd_normalize(cameraInScan - m.point)
        if let n = normal, simd_dot(n, toCamera) > cos(Float.pi * 70 / 180) {
            mat.readsFromDepthBuffer = true
            mat.writesToDepthBuffer = false
            node.simdOrientation = simd_quatf(from: SIMD3(0, 0, 1), to: n)
        } else {
            mat.readsFromDepthBuffer = false
            mat.writesToDepthBuffer = false
            node.constraints = [SCNBillboardConstraint()]
        }
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
        ambient.intensity = 620
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
        // Studio fill and rim (soft, no shadows): the fill lifts the side away from the key, the rim from behind and
        // above separates the object's edge from the dark stage.
        let fill = SCNLight()
        fill.type = .directional
        fill.intensity = 170
        fill.color = CGColor(gray: 1, alpha: 1)
        let f = SCNNode()
        f.light = fill
        f.simdEulerAngles = SIMD3(-0.25, 0.75, 0)
        cameraNode.addChildNode(f)
        let rim = SCNLight()
        rim.type = .directional
        rim.intensity = 230
        rim.color = CGColor(srgbRed: 0.93, green: 0.91, blue: 1, alpha: 1)
        let r = SCNNode()
        r.light = rim
        r.simdEulerAngles = SIMD3(-0.9, .pi, 0)
        cameraNode.addChildNode(r)
    }

    /// Fragment shader modifier: fades the table patch around the object (horizontal distance from the focus centre in
    /// the model's frame, which is the scan frame) into the stage.
    static let fadeModifier = """
    #pragma arguments
    float3 fadeCenter;
    float fadeInner;
    float fadeOuter;
    float3 hlCenter;
    float hlRadius;
    float hlAmount;
    float3 hl2Center;
    float hl2Radius;
    float hl2Amount;
    float3 hlColor;
    #pragma transparent
    #pragma body
    float4 mp = scn_node.inverseModelViewTransform * float4(_surface.position, 1.0);
    // Lea's highlight: the part lights up on the surface, a gentle lift inside and a soft violet rim, falling off.
    float d1 = distance(mp.xyz, hlCenter);
    float in1 = (1.0 - smoothstep(hlRadius * 0.6, hlRadius, d1)) * hlAmount * step(0.0001, hlRadius);
    float rim1 = smoothstep(hlRadius * 0.62, hlRadius * 0.9, d1) * (1.0 - smoothstep(hlRadius * 0.9, hlRadius * 1.12, d1)) * hlAmount * step(0.0001, hlRadius);
    float d2 = distance(mp.xyz, hl2Center);
    float in2 = (1.0 - smoothstep(hl2Radius * 0.6, hl2Radius, d2)) * hl2Amount * step(0.0001, hl2Radius);
    float rim2 = smoothstep(hl2Radius * 0.62, hl2Radius * 0.9, d2) * (1.0 - smoothstep(hl2Radius * 0.9, hl2Radius * 1.12, d2)) * hl2Amount * step(0.0001, hl2Radius);
    float lift = max(in1, in2);
    float rim = max(rim1, rim2);
    _output.color.rgb = _output.color.rgb * (1.0 + 0.22 * lift) + hlColor * (0.6 * rim) * _output.color.a;
    float r = length(mp.xz - fadeCenter.xz);
    float a = 1.0 - smoothstep(fadeInner, fadeOuter, r);
    _output.color *= a;
    """

    /// Fragment shader modifier for a ring that draws on: `drawn` 0...1 of the circle, clockwise from the top.
    static let drawOnModifier = """
    #pragma arguments
    float drawn;
    #pragma body
    float2 p = _surface.diffuseTexcoord - float2(0.5, 0.5);
    float a = atan2(p.x, -p.y) / 6.2831853;
    a = a < 0.0 ? a + 1.0 : a;
    if (a > drawn) { discard_fragment(); }
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

    /// The ring texture (Design/LeaStageStyle `StageMarkImages`): the tone over a keyline, dashed for a mark without a
    /// number; the circle at 1/1.3 of the image. Drawn once per colour and style.
    static func ring(rgb: SIMD3<Float>, dashed: Bool) -> CGImage? {
        StageMarkImages.ring(rgb: rgb, dashed: dashed)
    }

    /// The shared `Marker`: a dark disc outlined in the tone, the number in the display face (CoreText, so it draws the
    /// same everywhere).
    static func badge(_ m: RecreationMarker) -> CGImage? {
        StageMarkImages.badge(number: m.number, rgb: m.rgb, side: m.number == nil ? 48 : 96)
    }
}

extension RecreationMarker {
    /// The report's tone colours (Design/Theme.swift `Tone`), as plain sRGB for SceneKit.
    static func rgb(tone: String) -> SIMD3<Float> {
        switch tone {
        case "danger": StageMarkImages.rgb(.danger)
        case "caution": StageMarkImages.rgb(.caution)
        case "good": StageMarkImages.rgb(.good)
        case "cosmetic": StageMarkImages.rgb(.cosmetic)
        case "setup": StageMarkImages.rgb(.setup)
        default: StageMarkImages.rgb(.neutral)
        }
    }
}
