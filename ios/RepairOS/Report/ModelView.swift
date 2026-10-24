import SceneKit
import SwiftUI

// The scanned object in 3D with a pin on each located finding. SceneKit: a native point primitive for the cloud,
// USDZ/OBJ loading for a mesh, and on-demand rendering (`rendersContinuously = false`), so the view draws only
// while someone turns it or the camera flies to a pin. Shown on the report screen only.

/// A finding placed on the scan.
struct ModelPin: Identifiable, Sendable, Equatable {
    var id: String
    /// Shown in the pin; nil draws a small unnumbered dot (lower-ranked findings).
    var number: Int?
    var point: SIMD3<Float>
    var tone: Tone
}

/// Points to draw (already capped), metres, the scan's ARKit frame, Y up.
struct ScanCloud: Sendable {
    var positions: [SIMD3<Float>]
    var colors: [SIMD3<Float>]?
}

/// Where the 3D geometry comes from.
enum ModelSource: Sendable {
    case none
    case pointCloud(URL)
    case mesh(URL)
    case points(ScanCloud)

    /// The session's own files: the PLY the locator ray-casts against (same frame as `location.point_m`), else the
    /// photogrammetry mesh. A later segment's own cloud (`points_segmentN_…`, an unaligned continuation) is in
    /// another frame, so it is never the one drawn under the pins.
    static func find(in scanDir: URL) -> ModelSource {
        let fm = FileManager.default
        let names = ((try? fm.contentsOfDirectory(atPath: scanDir.path)) ?? []).sorted()
        let main = "points_arkit_world.ply"
        if let ply = names.contains(main) ? main : names.first(where: { $0.hasSuffix(".ply") && !$0.hasPrefix("points_segment") }) {
            return .pointCloud(scanDir.appending(path: ply))
        }
        if let mesh = names.first(where: { $0.hasSuffix(".usdz") || $0.hasSuffix(".obj") }) { return .mesh(scanDir.appending(path: mesh)) }
        return .none
    }

    var isNone: Bool { if case .none = self { true } else { false } }
}

// MARK: - PLY

/// Reads a binary little-endian PLY's vertex element: x, y, z (float or double) and red/green/blue (uchar) when
/// present. Keeps at most `maxPoints` by even striding.
enum PLYReader {
    enum Failure: Error { case notPLY, unsupported(String) }

    static func read(_ data: Data, maxPoints: Int = 200_000) throws -> ScanCloud {
        guard let end = data.range(of: Data("end_header\n".utf8)) else { throw Failure.notPLY }
        let header = String(decoding: data[..<end.lowerBound], as: UTF8.self).split(separator: "\n")
        guard header.first == "ply" else { throw Failure.notPLY }
        guard header.contains(where: { $0.hasPrefix("format binary_little_endian") }) else { throw Failure.unsupported("format") }
        var count = 0, inVertex = false, offset = 0, firstElement = true
        var fields: [String: (offset: Int, type: String)] = [:]
        for line in header {
            let w = line.split(separator: " ").map(String.init)
            if w.first == "element" {
                if !firstElement && w.count > 1 && w[1] == "vertex" { throw Failure.unsupported("vertex element not first") }
                inVertex = w.count > 2 && w[1] == "vertex"
                if inVertex { count = Int(w[2]) ?? 0 }
                firstElement = false
            } else if w.first == "property", inVertex, w.count == 3 {
                guard let size = Self.sizes[w[1]] else { throw Failure.unsupported(w[1]) }
                fields[w[2]] = (offset, w[1])
                offset += size
            }
        }
        guard let x = fields["x"], let y = fields["y"], let z = fields["z"], count > 0 else { throw Failure.unsupported("no xyz") }
        let stride = offset, start = end.upperBound - data.startIndex
        guard data.count >= start + count * stride else { throw Failure.unsupported("truncated") }
        let step = max(1, Int((Double(count) / Double(maxPoints)).rounded(.up)))
        let rgb = [fields["red"], fields["green"], fields["blue"]]
        let hasColor = rgb.allSatisfy { $0?.type == "uchar" || $0?.type == "uint8" }
        var positions: [SIMD3<Float>] = [], colors: [SIMD3<Float>] = []
        positions.reserveCapacity(count / step + 1)
        data.withUnsafeBytes { raw in
            func float(_ base: Int, _ f: (offset: Int, type: String)) -> Float {
                f.type == "double" || f.type == "float64"
                    ? Float(raw.loadUnaligned(fromByteOffset: base + f.offset, as: Double.self))
                    : raw.loadUnaligned(fromByteOffset: base + f.offset, as: Float.self)
            }
            for i in Swift.stride(from: 0, to: count, by: step) {
                let base = start + i * stride
                positions.append(SIMD3(float(base, x), float(base, y), float(base, z)))
                if hasColor {
                    colors.append(SIMD3(rgb.map { Float(raw[base + $0!.offset]) / 255 }))
                }
            }
        }
        return ScanCloud(positions: positions, colors: hasColor ? colors : nil)
    }

    private static let sizes: [String: Int] = ["char": 1, "uchar": 1, "int8": 1, "uint8": 1, "short": 2, "ushort": 2,
                                               "int16": 2, "uint16": 2, "int": 4, "uint": 4, "int32": 4, "uint32": 4,
                                               "float": 4, "float32": 4, "double": 8, "float64": 8]
}

// MARK: - View

/// The 3D card: drag to turn, pinch to zoom, tap a pin. Setting `focus` flies the camera to that pin.
struct ModelView: View {
    let source: ModelSource
    let pins: [ModelPin]
    var focus: String?
    let onPinTap: (String) -> Void

    @State private var loaded: Loaded?
    @State private var failed = false
    @State private var recenter = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    enum Loaded { case cloud(ScanCloud), mesh(URL), nothing }

    var body: some View {
        ZStack {
            Palette.well
            if let loaded {
                SceneHost(content: loaded, pins: pins, focus: focus, recenter: recenter, animate: !reduceMotion, onPinTap: onPinTap)
                    .transition(.opacity)
            } else if !failed {
                ProgressView()
            }
            if failed || (loaded.map { if case .nothing = $0 { true } else { false } } ?? false) && pins.isEmpty {
                Label("No 3D scan for this report", systemImage: "cube.transparent")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
        .overlay(alignment: .topTrailing) {
            Button { recenter += 1 } label: { Image(systemName: "scope") }
                .buttonStyle(RoundIconButtonStyle())
                .padding(Spacing.s)
                .accessibilityLabel("Show the whole object")
        }
        .overlay(alignment: .bottomLeading) {
            StatusPill(text: "Drag to turn · pinch to zoom", symbol: "rotate.3d")
                .dynamicTypeSize(...DynamicTypeSize.xxLarge)
                .background(Palette.surface.opacity(0.9), in: Capsule())
                .padding(Spacing.m).allowsHitTesting(false)
        }
        .task(id: Self.key(source)) { await load() }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("3D view of the scanned object with \(pins.count) problem pins")
    }

    private func load() async {
        switch source {
        case .none: loaded = .nothing
        case .points(let cloud): loaded = .cloud(cloud)
        case .mesh(let url): loaded = .mesh(url)
        case .pointCloud(let url):
            if let cloud = await Self.readCloud(url) {
                withAnimation(Motion.fade) { loaded = .cloud(cloud) }
            } else {
                failed = true
                loaded = .nothing
            }
        }
    }

    @concurrent
    private static func readCloud(_ url: URL) async -> ScanCloud? {
        guard let data = try? Data(contentsOf: url, options: .mappedIfSafe) else { return nil }
        return try? PLYReader.read(data)
    }

    private static func key(_ s: ModelSource) -> String {
        switch s {
        case .none: "none"
        case .pointCloud(let u), .mesh(let u): u.path
        case .points(let c): "points:\(c.positions.count)"
        }
    }
}

private struct SceneHost: UIViewRepresentable {
    let content: ModelView.Loaded
    let pins: [ModelPin]
    let focus: String?
    let recenter: Int
    let animate: Bool
    let onPinTap: (String) -> Void

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> SCNView {
        let view = SCNView(frame: .zero)
        view.backgroundColor = .clear
        view.antialiasingMode = .multisampling2X
        view.rendersContinuously = false
        view.preferredFramesPerSecond = 60
        view.allowsCameraControl = true
        view.defaultCameraController.interactionMode = .orbitTurntable
        view.defaultCameraController.inertiaEnabled = true
        view.scene = SCNScene()
        view.addGestureRecognizer(UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.tap(_:))))
        context.coordinator.view = view
        context.coordinator.build(content, pins: pins, traits: view.traitCollection)
        return view
    }

    func updateUIView(_ view: SCNView, context: Context) {
        let c = context.coordinator
        c.onPinTap = onPinTap
        if c.pins != pins {
            c.pins = pins
            c.placePins(traits: view.traitCollection)
        }
        if c.focus != focus {
            c.focus = focus
            if let focus { c.fly(to: focus, animate: animate) }
        }
        if c.recenter != recenter {
            c.recenter = recenter
            c.frameAll(animate: animate)
        }
    }

    @MainActor
    final class Coordinator: NSObject {
        weak var view: SCNView?
        var pins: [ModelPin] = []
        var focus: String?
        var recenter = 0
        var onPinTap: (String) -> Void = { _ in }
        private let modelNode = SCNNode()
        private let pinRoot = SCNNode()
        private let cameraNode = SCNNode()
        private var center = SIMD3<Float>(0, 0, 0)
        private var radius: Float = 0.1

        func build(_ content: ModelView.Loaded, pins: [ModelPin], traits: UITraitCollection) {
            guard let scene = view?.scene else { return }
            self.pins = pins
            switch content {
            case .cloud(let cloud): modelNode.geometry = Self.geometry(cloud)
            case .mesh(let url):
                if let mesh = try? SCNScene(url: url) { mesh.rootNode.childNodes.forEach { modelNode.addChildNode($0) } }
                view?.autoenablesDefaultLighting = true
            case .nothing: break
            }
            scene.rootNode.addChildNode(modelNode)
            scene.rootNode.addChildNode(pinRoot)
            let camera = SCNCamera()
            camera.fieldOfView = 38
            camera.zNear = 0.001
            camera.zFar = 100
            cameraNode.camera = camera
            scene.rootNode.addChildNode(cameraNode)
            view?.pointOfView = cameraNode
            measure()
            placePins(traits: traits)
            frameAll(animate: false)
        }

        /// Bounds of the model and the pins together, so a pin off the cloud is never out of view.
        private func measure() {
            var mn = SIMD3<Float>(repeating: .infinity), mx = -mn
            if modelNode.geometry != nil || !modelNode.childNodes.isEmpty {
                let (lo, hi) = modelNode.boundingBox
                mn = SIMD3(lo)
                mx = SIMD3(hi)
            }
            for p in pins { mn = simd_min(mn, p.point); mx = simd_max(mx, p.point) }
            guard mn.x.isFinite, mx.x >= mn.x else { return }
            center = (mn + mx) / 2
            radius = max(0.03, simd_length(mx - mn) / 2)
        }

        func placePins(traits: UITraitCollection) {
            pinRoot.childNodes.forEach { $0.removeFromParentNode() }
            for pin in pins {
                let size = CGFloat(radius) * (pin.number == nil ? 0.07 : 0.16)
                let plane = SCNPlane(width: size, height: size)
                let m = SCNMaterial()
                m.diffuse.contents = Self.badge(pin, traits: traits)
                m.lightingModel = .constant
                m.isDoubleSided = true
                m.readsFromDepthBuffer = false
                m.writesToDepthBuffer = false
                plane.materials = [m]
                let node = SCNNode(geometry: plane)
                node.name = "pin:\(pin.id)"
                node.simdPosition = pin.point
                node.renderingOrder = pin.number == nil ? 10 : 20 + (pin.number ?? 0)
                node.constraints = [SCNBillboardConstraint()]
                pinRoot.addChildNode(node)
            }
            highlight()
        }

        func frameAll(animate: Bool) {
            focus = nil
            move(target: center, distance: radius * 2.6, keepDirection: false, animate: animate)
            highlight()
        }

        func fly(to id: String, animate: Bool) {
            guard let pin = pins.first(where: { $0.id == id }) else { return }
            move(target: pin.point, distance: radius * 1.5, keepDirection: true, animate: animate)
            highlight()
        }

        private func move(target: SIMD3<Float>, distance: Float, keepDirection: Bool, animate: Bool) {
            var dir = simd_normalize(SIMD3<Float>(0.35, 0.55, 1))
            if keepDirection {
                let d = cameraNode.simdWorldPosition - target
                if simd_length(d) > 1e-4 { dir = simd_normalize(d) }
            }
            SCNTransaction.begin()
            SCNTransaction.animationDuration = animate ? 0.6 : 0
            SCNTransaction.animationTimingFunction = CAMediaTimingFunction(controlPoints: 0.32, 0.72, 0, 1)
            cameraNode.simdWorldPosition = target + dir * distance
            cameraNode.simdLook(at: target, up: SIMD3(0, 1, 0), localFront: SIMD3(0, 0, -1))
            SCNTransaction.commit()
            view?.defaultCameraController.target = SCNVector3(target)
        }

        private func highlight() {
            for node in pinRoot.childNodes {
                let on = node.name == focus.map { "pin:\($0)" }
                SCNTransaction.begin()
                SCNTransaction.animationDuration = 0.2
                node.simdScale = SIMD3(repeating: on ? 1.35 : 1)
                node.opacity = focus == nil || on ? 1 : 0.55
                SCNTransaction.commit()
            }
        }

        @objc func tap(_ g: UITapGestureRecognizer) {
            guard let view else { return }
            let hits = view.hitTest(g.location(in: view), options: [.searchMode: SCNHitTestSearchMode.all.rawValue,
                                                                     .ignoreHiddenNodes: true, .rootNode: pinRoot])
            if let name = hits.lazy.compactMap(\.node.name).first(where: { $0.hasPrefix("pin:") }) {
                onPinTap(String(name.dropFirst(4)))
            }
        }

        private static func geometry(_ cloud: ScanCloud) -> SCNGeometry {
            let n = cloud.positions.count
            let vertices = cloud.positions.withUnsafeBufferPointer { Data(buffer: $0) }
            let vsrc = SCNGeometrySource(data: vertices, semantic: .vertex, vectorCount: n, usesFloatComponents: true,
                                         componentsPerVector: 3, bytesPerComponent: 4, dataOffset: 0,
                                         dataStride: MemoryLayout<SIMD3<Float>>.stride)
            var sources = [vsrc]
            if let colors = cloud.colors, colors.count == n {
                let cdata = colors.withUnsafeBufferPointer { Data(buffer: $0) }
                sources.append(SCNGeometrySource(data: cdata, semantic: .color, vectorCount: n, usesFloatComponents: true,
                                                 componentsPerVector: 3, bytesPerComponent: 4, dataOffset: 0,
                                                 dataStride: MemoryLayout<SIMD3<Float>>.stride))
            }
            let indices = Array(0..<UInt32(n)).withUnsafeBufferPointer { Data(buffer: $0) }
            let element = SCNGeometryElement(data: indices, primitiveType: .point, primitiveCount: n, bytesPerIndex: 4)
            element.pointSize = 3
            element.minimumPointScreenSpaceRadius = 1.2
            element.maximumPointScreenSpaceRadius = 3.5
            let g = SCNGeometry(sources: sources, elements: [element])
            let m = SCNMaterial()
            m.lightingModel = .constant
            m.diffuse.contents = cloud.colors == nil ? UIColor(light: 0x8A8A96, dark: 0xB4B4C0) : UIColor.white
            g.materials = [m]
            return g
        }

        /// A round severity-coloured badge with the number (or a small dot), drawn once per pin.
        private static func badge(_ pin: ModelPin, traits: UITraitCollection) -> UIImage {
            let side: CGFloat = pin.number == nil ? 48 : 96
            let colour = UIColor(pin.tone.color).resolvedColor(with: traits)
            return UIGraphicsImageRenderer(size: CGSize(width: side, height: side)).image { ctx in
                let rect = CGRect(x: 0, y: 0, width: side, height: side)
                UIColor.white.setFill()
                ctx.cgContext.fillEllipse(in: rect)
                colour.setFill()
                ctx.cgContext.fillEllipse(in: rect.insetBy(dx: side * 0.08, dy: side * 0.08))
                guard let number = pin.number else { return }
                let text = "\(number)" as NSString
                let font = UIFont.systemFont(ofSize: side * 0.5, weight: .bold).withRounded
                let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: UIColor.white]
                let size = text.size(withAttributes: attrs)
                text.draw(at: CGPoint(x: (side - size.width) / 2, y: (side - size.height) / 2), withAttributes: attrs)
            }
        }
    }
}

private extension UIFont {
    var withRounded: UIFont {
        fontDescriptor.withDesign(.rounded).map { UIFont(descriptor: $0, size: pointSize) } ?? self
    }
}

// MARK: - Synthetic (previews)

#if DEBUG
enum SyntheticScan {
    /// A plain block (6 × 8 × 3 cm) with two pins on top, ~60k points, and three located findings on it.
    static let cloud: ScanCloud = {
        var p: [SIMD3<Float>] = [], c: [SIMD3<Float>] = []
        var rng = SystemRandomNumberGenerator()
        func r(_ a: Float, _ b: Float) -> Float { Float.random(in: a...b, using: &rng) }
        for _ in 0..<52_000 {
            // A point on the block's surface, shaded by the face it is on.
            let face = Int.random(in: 0..<6, using: &rng)
            var v = SIMD3(r(-0.03, 0.03), r(0, 0.08), r(-0.015, 0.015))
            switch face {
            case 0: v.x = -0.03
            case 1: v.x = 0.03
            case 2: v.y = 0
            case 3: v.y = 0.08
            case 4: v.z = -0.015
            default: v.z = 0.015
            }
            p.append(v)
            let shade: Float = [0.78, 0.84, 0.7, 0.95, 0.88, 0.92][face]
            c.append(SIMD3(repeating: shade))
        }
        for x in [Float(-0.012), 0.012] {
            for _ in 0..<4_000 {
                p.append(SIMD3(x + r(-0.0012, 0.0012), 0.08 + r(0, 0.018), r(-0.004, 0.004)))
                c.append(SIMD3(0.62, 0.62, 0.66))
            }
        }
        return ScanCloud(positions: p, colors: c)
    }()

    static let points: [SIMD3<Float>] = [SIMD3(-0.012, 0.094, 0), SIMD3(0.012, 0.096, 0.002), SIMD3(0.03, 0.02, 0.006)]
}

#Preview("3D model") {
    @Previewable @State var focus: String?
    let pins = zip(["a", "b", "c"], SyntheticScan.points).enumerated().map { i, e in
        ModelPin(id: e.0, number: i + 1, point: e.1, tone: i == 2 ? .caution : .danger)
    }
    VStack {
        ModelView(source: .points(SyntheticScan.cloud), pins: pins, focus: focus) { focus = $0 }
            .frame(height: 320).clipShape(RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
        HStack { ForEach(pins) { p in Button("\(p.number ?? 0)") { focus = p.id } } }
    }
    .padding()
}
#endif
