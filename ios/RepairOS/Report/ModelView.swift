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
    /// One of Lea's rings on a part that is not a numbered problem: a solid ring of this radius (metres).
    var ringRadius: Float? = nil
    /// A finding's problem area on the scan (metres): the recreation circles it. Not one of Lea's rings (`ringRadius`).
    var area: Float? = nil
    /// The part's name while Lea is showing it: drawn as a label on the model, out along the surface normal.
    var label: String? = nil
}

/// Points to draw (already capped), metres, the scan's ARKit frame, Y up.
struct ScanCloud: Sendable {
    var positions: [SIMD3<Float>]
    var colors: [SIMD3<Float>]?
}

extension ScanCloud {
    /// The bulk of the cloud: the per-axis median and the 80th-percentile distance from it, so stray points (the
    /// table edge, a far wall) never shrink the object on screen.
    var bulk: (center: SIMD3<Float>, radius: Float) {
        guard !positions.isEmpty else { return (.zero, 0.1) }
        let step = max(1, positions.count / 20_000)
        let sample = stride(from: 0, to: positions.count, by: step).map { positions[$0] }
        var c = SIMD3<Float>()
        for axis in 0..<3 { let v = sample.map { $0[axis] }.sorted(); c[axis] = v[v.count / 2] }
        let d = sample.map { simd_distance($0, c) }.sorted()
        return (c, max(0.03, d[Int(Double(d.count - 1) * 0.8)]))
    }
}

/// Where the 3D geometry comes from.
enum ModelSource: Sendable {
    case none
    case pointCloud(URL)
    case mesh(URL)
    /// A binary PLY triangle mesh in the scan's ARKit frame (the gateway's `GET /v1/scans/{id}/mesh`), cropped to the
    /// scan's locked box when there is one (the mesh can include the table and walls around the object).
    case meshPLY(URL, crop: ObjectBox?)
    case points(ScanCloud)
    /// The backend says photos read better than 3D (a small object): the photo hero.
    case photos

    /// The backend mesh and cleaned cloud, cached next to the scan once fetched.
    static let backendMeshName = "mesh_gateway.ply"
    static let backendCloudName = "cloud_gateway.ply"
    static let gatewayFiles: Set<String> = [backendMeshName, backendCloudName]

    /// What the hero shows, following the backend's choice (`hero`: points, mesh, photos) once it has made one; until
    /// then the gateway mesh if fetched, else the phone's own cloud as a placeholder.
    static func find(in scanDir: URL, hero: String?) -> ModelSource {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: scanDir.path)) ?? []
        switch hero {
        case "photos": return .photos
        case "points" where names.contains(backendCloudName): return .pointCloud(scanDir.appending(path: backendCloudName))
        case "mesh" where names.contains(backendMeshName):
            return .meshPLY(scanDir.appending(path: backendMeshName), crop: cropBox(in: scanDir))
        default: return find(in: scanDir)
        }
    }

    /// The session's own files: the gateway's mesh (same frame as `location.point_m`) when it has been fetched, else
    /// the PLY cloud the locator ray-casts against (ownCloud), else a photogrammetry mesh.
    static func find(in scanDir: URL) -> ModelSource {
        let fm = FileManager.default
        let names = ((try? fm.contentsOfDirectory(atPath: scanDir.path)) ?? []).sorted()
        if names.contains(backendMeshName) { return .meshPLY(scanDir.appending(path: backendMeshName), crop: cropBox(in: scanDir)) }
        if let ply = ownCloud(in: names) {
            return .pointCloud(scanDir.appending(path: ply))
        }
        if let mesh = names.first(where: { $0.hasSuffix(".usdz") || $0.hasSuffix(".obj") }) { return .mesh(scanDir.appending(path: mesh)) }
        return .none
    }

    var isNone: Bool { if case .none = self { true } else { false } }
    var isPhotos: Bool { if case .photos = self { true } else { false } }

    /// The scan package's `crop_box` (the locked object) as a box.
    static func cropBox(in scanDir: URL) -> ObjectBox? {
        guard let data = try? Data(contentsOf: scanDir.appending(path: "scan_package.json")),
              let pkg = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let cb = pkg["crop_box"] as? [String: Any],
              let c = (cb["center_m"] as? [NSNumber])?.map(\.floatValue), c.count == 3,
              let e = (cb["extent_m"] as? [NSNumber])?.map(\.floatValue), e.count == 3 else { return nil }
        let q = (cb["quaternion_xyzw"] as? [NSNumber])?.map(\.floatValue) ?? [0, 0, 0, 1]
        let yaw = q.count == 4 ? 2 * atan2(q[1], q[3]) : 0
        return ObjectBox(center: SIMD3(c[0], c[1], c[2]), size: SIMD3(e[0], e[1], e[2]), yaw: yaw)
    }

    /// The scan's own PLY cloud (the frame the locator ray-casts against): `points_arkit_world.ply` when present, else
    /// the first other PLY that is neither a gateway file nor a later segment's own cloud (`points_segmentN_…`, an
    /// unaligned continuation in another frame, so never the one drawn under the pins). `names` sorted.
    static func ownCloud(in names: [String]) -> String? {
        let main = "points_arkit_world.ply"
        if names.contains(main) { return main }
        return names.first(where: { $0.hasSuffix(".ply") && !gatewayFiles.contains($0) && !$0.hasPrefix("points_segment") })
    }

    /// The point cloud to fall back to when a mesh cannot be read.
    static func cloudFallback(in scanDir: URL) -> URL? {
        let names = ((try? FileManager.default.contentsOfDirectory(atPath: scanDir.path)) ?? []).sorted()
        return ownCloud(in: names).map { scanDir.appending(path: $0) }
    }
}

/// A triangle mesh to draw: positions, optional colours, normals (computed), triangle indices.
struct ScanMesh: Sendable {
    var positions: [SIMD3<Float>]
    var colors: [SIMD3<Float>]?
    var normals: [SIMD3<Float>]
    var indices: [UInt32]

    /// Triangles with every corner inside `box` (+ `margin`) and above its floor (the support plane + `floor`), with
    /// unused vertices dropped: the object without the table or the room.
    func cropped(to box: ObjectBox, margin: Float, floor: Float) -> ScanMesh {
        let keep = positions.map { box.contains($0, margin: margin) && $0.y > box.planeY + floor }
        var remap = [Int32](repeating: -1, count: positions.count)
        var p: [SIMD3<Float>] = [], n: [SIMD3<Float>] = [], c: [SIMD3<Float>] = [], idx: [UInt32] = []
        for t in stride(from: 0, to: indices.count, by: 3) {
            let tri = [Int(indices[t]), Int(indices[t + 1]), Int(indices[t + 2])]
            guard tri.allSatisfy({ keep[$0] }) else { continue }
            for v in tri {
                if remap[v] < 0 {
                    remap[v] = Int32(p.count)
                    p.append(positions[v]); n.append(normals[v])
                    if let colors { c.append(colors[v]) }
                }
                idx.append(UInt32(remap[v]))
            }
        }
        return ScanMesh(positions: p, colors: colors == nil ? nil : c, normals: n, indices: idx)
    }
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

    /// A binary little-endian triangle mesh: the vertex element (x, y, z, optional uchar red/green/blue), then a face
    /// element with a `list uchar int|uint vertex_indices` property (triangles; larger polygons are fanned). Nil when
    /// the file has no faces or more than `maxFaces` (the caller then draws the vertices as points).
    static func readMesh(_ data: Data, maxFaces: Int = 400_000) throws -> ScanMesh? {
        guard let end = data.range(of: Data("end_header\n".utf8)) else { throw Failure.notPLY }
        let header = String(decoding: data[..<end.lowerBound], as: UTF8.self).split(separator: "\n").map(String.init)
        guard header.first == "ply", header.contains(where: { $0.hasPrefix("format binary_little_endian") }) else { throw Failure.notPLY }
        var element = "", vCount = 0, fCount = 0, vStride = 0
        var fields: [String: (offset: Int, type: String)] = [:]
        var faceList: (count: String, index: String)?
        var faceExtra = 0
        for line in header {
            let w = line.split(separator: " ").map(String.init)
            if w.first == "element", w.count > 2 {
                element = w[1]
                if element == "vertex" { vCount = Int(w[2]) ?? 0 }
                if element == "face" { fCount = Int(w[2]) ?? 0 }
            } else if w.first == "property", element == "vertex", w.count == 3 {
                guard let size = sizes[w[1]] else { throw Failure.unsupported(w[1]) }
                fields[w[2]] = (vStride, w[1])
                vStride += size
            } else if w.first == "property", element == "face" {
                if w.count == 5, w[1] == "list" { faceList = (w[2], w[3]) } else if w.count == 3 { faceExtra += sizes[w[1]] ?? 0 }
            }
        }
        guard let x = fields["x"], let y = fields["y"], let z = fields["z"], vCount > 0 else { throw Failure.unsupported("no xyz") }
        guard let list = faceList, fCount > 0, fCount <= maxFaces, let countSize = sizes[list.count], let indexSize = sizes[list.index],
              indexSize == 4, faceExtra == 0 else { return nil }
        let start = end.upperBound - data.startIndex
        guard data.count >= start + vCount * vStride else { throw Failure.unsupported("truncated") }
        let rgb = [fields["red"], fields["green"], fields["blue"]]
        let hasColor = rgb.allSatisfy { $0?.type == "uchar" || $0?.type == "uint8" }
        var positions = [SIMD3<Float>](), colors = [SIMD3<Float>](), indices = [UInt32]()
        positions.reserveCapacity(vCount)
        indices.reserveCapacity(fCount * 3)
        var ok = true
        data.withUnsafeBytes { raw in
            func float(_ base: Int, _ f: (offset: Int, type: String)) -> Float {
                f.type == "double" || f.type == "float64"
                    ? Float(raw.loadUnaligned(fromByteOffset: base + f.offset, as: Double.self))
                    : raw.loadUnaligned(fromByteOffset: base + f.offset, as: Float.self)
            }
            for i in 0..<vCount {
                let base = start + i * vStride
                positions.append(SIMD3(float(base, x), float(base, y), float(base, z)))
                if hasColor { colors.append(SIMD3(rgb.map { Float(raw[base + $0!.offset]) / 255 })) }
            }
            var o = start + vCount * vStride
            for _ in 0..<fCount {
                guard o + countSize <= raw.count else { ok = false; return }
                let n = countSize == 1 ? Int(raw[o]) : Int(raw.loadUnaligned(fromByteOffset: o, as: UInt32.self))
                o += countSize
                guard n >= 3, o + n * 4 <= raw.count else { ok = false; return }
                let first = raw.loadUnaligned(fromByteOffset: o, as: UInt32.self)
                for k in 1..<(n - 1) {
                    let b = raw.loadUnaligned(fromByteOffset: o + k * 4, as: UInt32.self)
                    let c = raw.loadUnaligned(fromByteOffset: o + (k + 1) * 4, as: UInt32.self)
                    if first < vCount, b < vCount, c < vCount { indices.append(contentsOf: [first, b, c]) }
                }
                o += n * 4
            }
        }
        guard ok, !indices.isEmpty else { return nil }
        // Smooth vertex normals (area-weighted face normals).
        var normals = [SIMD3<Float>](repeating: .zero, count: vCount)
        for t in stride(from: 0, to: indices.count, by: 3) {
            let a = Int(indices[t]), b = Int(indices[t + 1]), c = Int(indices[t + 2])
            let n = simd_cross(positions[b] - positions[a], positions[c] - positions[a])
            normals[a] += n; normals[b] += n; normals[c] += n
        }
        for i in 0..<vCount { normals[i] = simd_length(normals[i]) > 1e-12 ? simd_normalize(normals[i]) : SIMD3(0, 1, 0) }
        return ScanMesh(positions: positions, colors: hasColor ? colors : nil, normals: normals, indices: indices)
    }
}

// MARK: - View

/// The 3D card: drag to turn, pinch to zoom, tap a marker. Setting `focus` flies the camera to that marker.
/// `spin` turns the object slowly until the first touch (the report's hero); it pauses while off screen.
struct ModelView: View {
    let source: ModelSource
    let pins: [ModelPin]
    var focus: String?
    var spin = false
    var showHint = true
    /// On the report's dark stage (Report/Stage): no well behind the scan (the stage shows through), glass controls,
    /// markers that never overlap and that pop in one by one.
    var stage = false
    /// Called once the scan is on screen (or there is nothing to show), so a caller can time the landing to it.
    var onLoaded: (() -> Void)? = nil
    /// Lets a circle drawn over the view be turned into a spot on the scan (circle to inspect).
    var probe: StageProbe? = nil
    /// The recenter control (off where the view is only to look at, like the waiting stage).
    var controls = true
    /// The walkthrough playing on the stage: its pager drives the camera (TourCamera) and the spin stops.
    @ObservedOptional var tour: WalkthroughPlayer? = nil
    /// Lea's camera move while she answers (LeaStage): fly to a marker, look from a side, zoom.
    var cue: LeaStageCue? = nil
    let onPinTap: (String) -> Void

    /// Reduce Motion: the walkthrough card the view shows (it cuts there while faded out).
    @State private var cutShown = 0
    @State private var loaded: Loaded?
    @State private var failed = false
    @State private var recenter = 0
    @State private var visible = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    enum Loaded { case cloud(ScanCloud), mesh(URL), triangles(ScanMesh), nothing }

    var body: some View {
        let frame = tour.flatMap { $0.active ? $0.frame : nil }
        ZStack {
            if !stage { Palette.well }
            if let loaded {
                SceneHost(content: loaded, pins: pins, focus: focus, recenter: recenter, animate: !reduceMotion,
                          spin: spin && !reduceMotion, active: visible, stage: stage, probe: probe,
                          tour: frame?.drawn(reduceMotion: reduceMotion, shown: cutShown), cue: cue, onPinTap: onPinTap)
                    .id(Self.loadedID(loaded))
                    .modifier(TourCutFade(nearest: frame?.nearest, on: reduceMotion, shown: $cutShown))
                    .transition(.opacity)
            } else if !failed {
                ProgressView().tint(stage ? Palette.chalk : nil)
            }
            if failed || (loaded.map { if case .nothing = $0 { true } else { false } } ?? false) && pins.isEmpty {
                Label("No 3D scan for this report", systemImage: "cube.transparent")
                    .font(.footnote).foregroundStyle(stage ? AnyShapeStyle(Palette.chalk.opacity(0.7)) : AnyShapeStyle(Palette.secondary))
            }
        }
        .overlay(alignment: .topTrailing) {
            if !stage {
                Button { recenter += 1 } label: { Image(systemName: "scope") }
                    .buttonStyle(RoundIconButtonStyle())
                    .padding(Spacing.s)
                    .accessibilityLabel("Show the whole object")
            }
        }
        .overlay(alignment: .bottomTrailing) {
            // On the stage the recenter control sits low, clear of the navigation bar drawn over the stage's top.
            if stage && controls {
                Button { recenter += 1 } label: { Image(systemName: "scope") }
                    .buttonStyle(StageIconButtonStyle())
                    .padding(Spacing.m)
                    .accessibilityLabel("Show the whole object")
            }
        }
        .overlay(alignment: .bottomLeading) {
            if showHint && !stage {
                StatusPill(text: "Drag to turn · pinch to zoom", symbol: "rotate.3d")
                    .dynamicTypeSize(...DynamicTypeSize.xxLarge)
                    .background(Palette.surface.opacity(0.9), in: Capsule())
                    .padding(Spacing.m).allowsHitTesting(false)
            }
        }
        .tracksVisibility($visible)
        .task(id: Self.key(source)) { await load(); onLoaded?() }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("3D view of the scanned object with \(pins.count) numbered problems")
    }

    private func load() async {
        switch source {
        case .none, .photos: loaded = .nothing
        case .points(let cloud): loaded = .cloud(cloud)
        case .mesh(let url): loaded = .mesh(url)
        case .meshPLY(let url, let crop):
            if let mesh = await Self.readMesh(url, crop: crop) {
                withAnimation(Motion.fade) { loaded = .triangles(mesh) }
            } else if let cloud = await Self.readCloud(url) {
                // Too big (or no faces) for triangles: its vertices as a cloud, capped like any cloud.
                withAnimation(Motion.fade) { loaded = .cloud(cloud) }
            } else if let fallback = ModelSource.cloudFallback(in: url.deletingLastPathComponent()),
                      let cloud = await Self.readCloud(fallback) {
                withAnimation(Motion.fade) { loaded = .cloud(cloud) }
            } else {
                failed = true
                loaded = .nothing
            }
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

    /// The mesh, cropped to the object's box; nil when too little of it is left there (the cloud is shown instead).
    @concurrent
    private static func readMesh(_ url: URL, crop: ObjectBox?) async -> ScanMesh? {
        guard let data = try? Data(contentsOf: url, options: .mappedIfSafe),
              let mesh = (try? PLYReader.readMesh(data)) ?? nil else { return nil }
        guard let crop else { return mesh }
        let cropped = mesh.cropped(to: crop, margin: 0.015, floor: 0.004)
        return cropped.indices.count / 3 >= minTriangles ? cropped : nil
    }

    /// Fewer triangles than this on the object: the mesh is not worth showing over the cloud.
    nonisolated static let minTriangles = 800

    /// A new geometry (the gateway mesh replacing the cloud) builds a new scene.
    private static func loadedID(_ l: Loaded) -> String {
        switch l {
        case .cloud(let c): "cloud:\(c.positions.count)"
        case .mesh(let u): "mesh:\(u.path)"
        case .triangles(let m): "tri:\(m.indices.count)"
        case .nothing: "nothing"
        }
    }

    private static func key(_ s: ModelSource) -> String {
        switch s {
        case .none: "none"
        case .photos: "photos"
        case .pointCloud(let u), .mesh(let u), .meshPLY(let u, _): u.path
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
    let spin: Bool
    let active: Bool
    var stage = false
    var probe: StageProbe?
    /// The walkthrough's frame: the camera follows its pager instead of `focus`.
    var tour: TourFrame?
    var cue: LeaStageCue?
    let onPinTap: (String) -> Void

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> SCNView {
        context.coordinator.cueN = cue?.n   // a move from before this view existed is not replayed
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
        // Any touch stops the hero spin (recognised alongside the camera controller's own gestures).
        let touch = UILongPressGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.touched(_:)))
        touch.minimumPressDuration = 0
        touch.cancelsTouchesInView = false
        touch.delegate = context.coordinator
        view.addGestureRecognizer(touch)
        context.coordinator.view = view
        context.coordinator.stage = stage
        context.coordinator.animateIn = stage && animate
        if stage { view.delegate = context.coordinator.separator }
        context.coordinator.build(content, pins: pins, traits: view.traitCollection)
        if spin { context.coordinator.startSpin() }
        return view
    }

    func updateUIView(_ view: SCNView, context: Context) {
        let c = context.coordinator
        c.onPinTap = onPinTap
        if c.pins != pins {
            c.pins = pins
            c.placePins(traits: view.traitCollection)
        }
        if let tour {
            c.setActive(active)
            c.tour(tour, animate: animate)
            return
        }
        if c.touring {
            // The tour ended: the markers as they were, and the whole object (or the focus) in view again.
            c.endTour()
            if let focus { c.fly(to: focus, animate: animate) } else { c.frameAll(animate: animate) }
            c.focus = focus
        }
        if c.focus != focus {
            c.focus = focus
            if let focus { c.stopSpin(); c.fly(to: focus, animate: animate) }
        }
        if c.recenter != recenter {
            c.recenter = recenter
            c.stopSpin()
            c.frameAll(animate: animate)
        }
        if let cue, c.cueN != cue.n {
            c.cueN = cue.n
            c.lea(cue.command, animate: animate)
        }
        c.setActive(active)
    }

    @MainActor
    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        weak var view: SCNView?
        var pins: [ModelPin] = []
        var focus: String?
        var recenter = 0
        var onPinTap: (String) -> Void = { _ in }
        /// The report stage (RedesignFlags.reportStage): larger points on black, markers kept apart on screen.
        var stage = false
        /// Pop the markers in one by one the first time they are placed (off under Reduce Motion).
        var animateIn = false
        let separator = MarkerSeparator()
        /// Turns about the model's centre (the hero spin); `content` sits inside it, offset by −centre.
        private let pivot = SCNNode()
        private let content = SCNNode()
        private let modelNode = SCNNode()
        private let pinRoot = SCNNode()
        private let cameraNode = SCNNode()
        private var center = SIMD3<Float>(0, 0, 0)
        private var radius: Float = 0.1
        private var spinning = false
        /// For a cloud: frame its bulk rather than its bounding box.
        private var bulk: (center: SIMD3<Float>, radius: Float)?
        private var active = true

        func build(_ loaded: ModelView.Loaded, pins: [ModelPin], traits: UITraitCollection) {
            guard let scene = view?.scene else { return }
            self.pins = pins
            switch loaded {
            case .cloud(let cloud):
                modelNode.geometry = Self.geometry(cloud, stage: stage)
                bulk = cloud.bulk
            case .triangles(let mesh):
                modelNode.geometry = Self.geometry(mesh)
                view?.autoenablesDefaultLighting = true
            case .mesh(let url):
                if let mesh = try? SCNScene(url: url) { mesh.rootNode.childNodes.forEach { modelNode.addChildNode($0) } }
                view?.autoenablesDefaultLighting = true
            case .nothing: break
            }
            content.addChildNode(modelNode)
            content.addChildNode(pinRoot)
            pivot.addChildNode(content)
            scene.rootNode.addChildNode(pivot)
            let camera = SCNCamera()
            camera.fieldOfView = 38
            camera.zNear = 0.001
            camera.zFar = 100
            cameraNode.camera = camera
            scene.rootNode.addChildNode(cameraNode)
            view?.pointOfView = cameraNode
            measure()
            pivot.simdPosition = center
            content.simdPosition = -center
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
            if let bulk {
                // The cloud's bulk, widened just enough to keep every marker in view.
                center = bulk.center
                radius = pins.reduce(bulk.radius) { max($0, simd_distance($1.point, bulk.center)) }
            }
        }

        // MARK: Spin

        func startSpin() {
            guard !spinning else { return }
            spinning = true
            pivot.runAction(.repeatForever(.rotateBy(x: 0, y: -2 * .pi, z: 0, duration: 40)), forKey: "spin")
            pivot.isPaused = !active
            view?.isPlaying = active
        }

        func stopSpin() {
            guard spinning else { return }
            spinning = false
            pivot.removeAction(forKey: "spin")
            view?.isPlaying = false
        }

        /// Off screen: the spin pauses and nothing draws.
        func setActive(_ on: Bool) {
            guard on != active else { return }
            active = on
            guard spinning else { return }
            pivot.isPaused = !on
            view?.isPlaying = on
        }

        @objc func touched(_ g: UILongPressGestureRecognizer) {
            if g.state == .began { stopSpin() }
        }

        nonisolated func gestureRecognizer(_ g: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }

        // MARK: Pins

        func placePins(traits: UITraitCollection) {
            pinRoot.childNodes.forEach { $0.removeFromParentNode() }
            for pin in pins {
                let size = pin.ringRadius.map { CGFloat($0 * 2) } ?? CGFloat(radius) * (pin.number == nil ? 0.05 : 0.16)
                let plane = SCNPlane(width: size, height: size)
                let m = SCNMaterial()
                m.diffuse.contents = Self.badge(pin, traits: traits)
                m.lightingModel = .constant
                m.isDoubleSided = true
                m.readsFromDepthBuffer = false
                m.writesToDepthBuffer = false
                plane.materials = [m]
                let node = SCNNode(geometry: plane)
                node.name = "marker:\(pin.id)"
                node.simdPosition = pin.point
                // The top problem draws over the others where they overlap.
                node.renderingOrder = pin.number == nil ? 10 : 30 - (pin.number ?? 0)
                node.constraints = [SCNBillboardConstraint()]
                pinRoot.addChildNode(node)
            }
            if stage {
                let ranked = pins.enumerated().filter { $0.element.ringRadius == nil }.sorted { ($0.element.number ?? 99, $0.offset) < ($1.element.number ?? 99, $1.offset) }
                separator.set(ranked.compactMap { i, pin in
                    pinRoot.childNodes.first { $0.name == "marker:\(pin.id)" }
                        .map { ($0, Float(radius) * (pin.number == nil ? 0.05 : 0.16)) }
                })
            }
            highlight()
            if animateIn && !pins.isEmpty {
                // Markers arrive 1, 2, 3: from 0.6 scale and clear, Motion.stagger apart (never from scale 0).
                animateIn = false
                for (i, node) in pinRoot.childNodes.enumerated() {
                    let rest = node.simdScale, opacity = node.opacity
                    node.simdScale = rest * 0.6
                    node.opacity = 0
                    let grow = SCNAction.group([.scale(to: CGFloat(rest.x), duration: 0.32), .fadeOpacity(to: opacity, duration: 0.2)])
                    grow.timingMode = .easeOut
                    node.runAction(.sequence([.wait(duration: 0.15 + Motion.stagger * Double(i)), grow]))
                }
                // Actions only advance while the view plays; keep it playing for the pop, then back to on-demand.
                view?.isPlaying = true
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
                    guard let self, !self.spinning else { return }
                    self.view?.isPlaying = false
                }
            }
        }

        // MARK: Walkthrough

        private(set) var touring = false
        /// One pose per step, worked out when the tour starts (the spin is stopped, so they hold).
        private var tourPoses: [TourPose] = []
        private var tourFocuses: [String?]?
        private var tourCut: Int?
        /// The eased move onto the first card's view: frames that change nothing meanwhile do not cut it short.
        private var glide: (progress: Double, until: Date)?

        /// One frame of the tour: the camera on the pager's path between two steps' views, the markers lit by weight.
        func tour(_ f: TourFrame, animate: Bool) {
            guard let view else { return }
            var glideIn = false
            if !touring {
                touring = true
                stopSpin()
                view.allowsCameraControl = false
                glideIn = animate
            }
            if tourFocuses != f.focuses {
                tourFocuses = f.focuses
                tourPoses = f.focuses.map(tourPose)
            }
            guard let pose = TourCamera.pose(tourPoses, at: f.progress, around: center, cut: f.cut) else { return }
            if f.cut {
                if tourCut != f.nearest { tourCut = f.nearest; place(pose, animate: false) }
            } else if glideIn {
                place(pose, animate: true)
                glide = (f.progress, Date().addingTimeInterval(0.75))
            } else if let g = glide, g.until > Date(), g.progress == f.progress {
                // still easing in to the first card
            } else {
                glide = nil
                place(pose, animate: false)
            }
            emphasize(TourCamera.weights(f.focuses, at: f.progress, cut: f.cut))
            view.isPlaying = active
        }

        func endTour() {
            touring = false
            tourFocuses = nil
            tourCut = nil
            view?.allowsCameraControl = true
            view?.isPlaying = false
            highlight()
        }

        /// The view `fly(to:)` lands on for a marker, `frameAll` for nil.
        private func tourPose(_ id: String?) -> TourPose {
            if let id, let node = pinRoot.childNodes.first(where: { $0.name == "marker:\(id)" }) {
                let target = node.presentation.simdWorldPosition
                var dir = target - center
                dir.y = max(dir.y, 0) + 0.5 * simd_length(SIMD2(dir.x, dir.z))
                let from = simd_length(dir) > 1e-4 ? simd_normalize(dir) : simd_normalize(cameraNode.simdWorldPosition - target)
                return TourPose(eye: target + from * radius * (stage ? 2.2 : 1.5), look: target)
            }
            return TourPose(eye: center + simd_normalize(SIMD3<Float>(0.35, 0.55, 1)) * radius * 3.5, look: center)
        }

        private func place(_ pose: TourPose, animate: Bool) {
            SCNTransaction.begin()
            SCNTransaction.animationDuration = animate ? Motion.Scene.fly : 0
            SCNTransaction.animationTimingFunction = Motion.Scene.curve
            cameraNode.simdWorldPosition = pose.eye
            cameraNode.simdLook(at: pose.look, up: SIMD3(0, 1, 0), localFront: SIMD3(0, 0, -1))
            SCNTransaction.commit()
            view?.defaultCameraController.target = SCNVector3(pose.look)
        }

        /// The markers lit by weight (TourCamera.weights): 1 is the selected look, the rest dim as one lights up.
        private func emphasize(_ weights: [String: Float]) {
            let lit = weights.values.max() ?? 0
            for node in pinRoot.childNodes {
                guard let id = node.name.map({ String($0.dropFirst("marker:".count)) }) else { continue }
                let on = weights[id] ?? 0
                let rest: Float = node.renderingOrder == 10 ? 0.75 : 1
                node.opacity = CGFloat(rest * (0.45 + 0.55 * max(on, 1 - lit)))
                node.simdScale = SIMD3(repeating: 1 + 0.35 * on)
            }
        }

        // MARK: Lea

        /// The last of Lea's moves applied (LeaStageCue.n).
        var cueN: Int?
        /// The side the default framing looks from (`move`'s default direction).
        static let front = StageCamera.azimuth(SIMD3(0.35, 0.55, 1))

        /// One of Lea's moves: fly to a marker, look from a side (the spin turned back to rest first, so "front" is
        /// the default framing's side), turn round, zoom. Reduce Motion: a cut.
        func lea(_ command: StageCommand, animate: Bool) {
            guard let view else { return }
            stopSpin()
            let now = TourPose(eye: cameraNode.simdWorldPosition, look: SIMD3<Float>(view.defaultCameraController.target))
            let distance = radius * 3.5
            switch command {
            case .show(let id):
                fly(to: id, animate: animate)
                return
            case .view(.reset):
                rest(animate: animate)
                frameAll(animate: animate)
                return
            case .view(let side):
                if side != .around { rest(animate: animate) }
                if let pose = StageCamera.pose(side, current: now, center: center, distance: distance, front: Self.front) {
                    place(pose, animate: animate)
                }
            case .zoom(let zoomIn):
                place(StageCamera.zoom(now, zoomIn: zoomIn, minDistance: radius * 0.6, maxDistance: radius * 7),
                      animate: animate)
            case .point:
                return   // the stage turns a point into a marker first; the view flies with .show
            }
            view.isPlaying = true
            DispatchQueue.main.asyncAfter(deadline: .now() + Motion.Scene.fly + 0.1) { [weak self] in
                guard let self, !self.spinning else { return }
                self.view?.isPlaying = false
            }
        }

        /// The spin back to rest, with the camera's move.
        private func rest(animate: Bool) {
            SCNTransaction.begin()
            SCNTransaction.animationDuration = animate ? Motion.Scene.fly : 0
            pivot.simdOrientation = simd_quatf(angle: 0, axis: SIMD3(0, 1, 0))
            SCNTransaction.commit()
        }

        func frameAll(animate: Bool) {
            focus = nil
            move(target: center, distance: radius * 3.5, keepDirection: false, animate: animate)
            highlight()
        }

        func fly(to id: String, animate: Bool) {
            guard let node = pinRoot.childNodes.first(where: { $0.name == "marker:\(id)" }) else { return }
            // World position: the pivot may have turned the model.
            let target = node.presentation.simdWorldPosition
            // Come in from the side the marker faces (away from the model's centre), so it is not hidden behind it.
            var dir = target - center
            dir.y = max(dir.y, 0) + 0.5 * simd_length(SIMD2(dir.x, dir.z))
            let from = simd_length(dir) > 1e-4 ? simd_normalize(dir) : simd_normalize(cameraNode.simdWorldPosition - target)
            // On the stage the markers are already large on black; stop a little further out.
            move(target: target, distance: radius * (stage ? 2.2 : 1.5), direction: from, animate: animate)
            highlight()
        }

        private func move(target: SIMD3<Float>, distance: Float, keepDirection: Bool = false, direction: SIMD3<Float>? = nil,
                          animate: Bool) {
            var dir = direction ?? simd_normalize(SIMD3<Float>(0.35, 0.55, 1))
            if keepDirection {
                let d = cameraNode.simdWorldPosition - target
                if simd_length(d) > 1e-4 { dir = simd_normalize(d) }
            }
            SCNTransaction.begin()
            SCNTransaction.animationDuration = animate ? Motion.Scene.fly : 0
            SCNTransaction.animationTimingFunction = Motion.Scene.curve
            cameraNode.simdWorldPosition = target + dir * distance
            cameraNode.simdLook(at: target, up: SIMD3(0, 1, 0), localFront: SIMD3(0, 0, -1))
            SCNTransaction.commit()
            view?.defaultCameraController.target = SCNVector3(target)
        }

        private func highlight() {
            for node in pinRoot.childNodes {
                let on = node.name == focus.map { "marker:\($0)" }
                SCNTransaction.begin()
                SCNTransaction.animationDuration = 0.2
                node.simdScale = SIMD3(repeating: on ? 1.35 : 1)
                let rest: CGFloat = node.renderingOrder == 10 ? 0.75 : 1
                node.opacity = focus == nil || on ? rest : 0.45
                SCNTransaction.commit()
            }
        }

        @objc func tap(_ g: UITapGestureRecognizer) {
            guard let view else { return }
            let hits = view.hitTest(g.location(in: view), options: [.searchMode: SCNHitTestSearchMode.all.rawValue,
                                                                     .ignoreHiddenNodes: true, .rootNode: pinRoot])
            if let name = hits.lazy.compactMap(\.node.name).first(where: { $0.hasPrefix("marker:") }) {
                onPinTap(String(name.dropFirst("marker:".count)))
            }
        }

        private static func geometry(_ cloud: ScanCloud, stage: Bool = false) -> SCNGeometry {
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
            // On the dark stage slightly larger points read as a solid object rather than dust.
            element.maximumPointScreenSpaceRadius = stage ? 4.5 : 3.5
            let g = SCNGeometry(sources: sources, elements: [element])
            let m = SCNMaterial()
            m.lightingModel = .constant
            m.diffuse.contents = cloud.colors == nil ? UIColor(light: 0x8A919C, dark: 0xB4BCC8) : UIColor.white
            g.materials = [m]
            return g
        }

        private static func geometry(_ mesh: ScanMesh) -> SCNGeometry {
            let n = mesh.positions.count
            func source(_ v: [SIMD3<Float>], _ semantic: SCNGeometrySource.Semantic) -> SCNGeometrySource {
                SCNGeometrySource(data: v.withUnsafeBufferPointer { Data(buffer: $0) }, semantic: semantic, vectorCount: n,
                                  usesFloatComponents: true, componentsPerVector: 3, bytesPerComponent: 4, dataOffset: 0,
                                  dataStride: MemoryLayout<SIMD3<Float>>.stride)
            }
            var sources = [source(mesh.positions, .vertex), source(mesh.normals, .normal)]
            if let colors = mesh.colors, colors.count == n { sources.append(source(colors, .color)) }
            let element = SCNGeometryElement(data: mesh.indices.withUnsafeBufferPointer { Data(buffer: $0) },
                                             primitiveType: .triangles, primitiveCount: mesh.indices.count / 3, bytesPerIndex: 4)
            let g = SCNGeometry(sources: sources, elements: [element])
            let m = SCNMaterial()
            m.lightingModel = .lambert
            m.isDoubleSided = true
            m.diffuse.contents = mesh.colors == nil ? UIColor(light: 0xA9B0BA, dark: 0xB4BCC8) : UIColor.white
            g.materials = [m]
            return g
        }

        /// The shared `Marker` (or a small outlined dot), drawn once per pin; a ring pin draws the highlight ring.
        private static func badge(_ pin: ModelPin, traits: UITraitCollection) -> UIImage {
            let rgb = StageMarkImages.rgb(pin.tone)
            if pin.ringRadius != nil {
                return StageMarkImages.ring(rgb: rgb).map(UIImage.init(cgImage:)) ?? UIImage()
            }
            return StageMarkImages.badge(number: pin.number, rgb: rgb, side: pin.number == nil ? 48 : 96)
                .map(UIImage.init(cgImage:)) ?? UIImage()
        }
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

private struct ModelViewPreviewHost: View {
    @State private var focus: String?
    private let pins = zip(["a", "b", "c"], SyntheticScan.points).enumerated().map { i, e in
        ModelPin(id: e.0, number: i + 1, point: e.1, tone: i == 2 ? .caution : .danger)
    }
    var body: some View {
        VStack {
            ModelView(source: .points(SyntheticScan.cloud), pins: pins, focus: focus) { focus = $0 }
                .frame(height: 320).clipShape(RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
            HStack { ForEach(pins) { p in Button("\(p.number ?? 0)") { focus = p.id } } }
        }
        .padding()
    }
}

#Preview("3D model") { ModelViewPreviewHost() }
#endif
