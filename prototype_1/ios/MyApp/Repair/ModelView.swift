import RealityKit
import SwiftUI

/// The validated scan in 3D, aligned to the saved reference, with each geometry candidate drawn where it is.
/// Tapping a candidate selects it; the next spoken turn ("what's that?") tells Lea which one you mean.
struct ModelView: View {
    @EnvironmentObject private var sync: SessionSync
    @EnvironmentObject private var actions: LeaActions
    let reportId: String
    @State private var model: MeshView?
    @State private var failure: String?

    var body: some View {
        ZStack(alignment: .bottom) {
            if let model {
                RealityView { content in
                    content.add(Self.scene(model))
                }
                .realityViewCameraControls(.orbit)
                .gesture(SpatialTapGesture().targetedToAnyEntity().onEnded { value in
                    if value.entity.name.hasPrefix("finding:") { actions.focusFindingId = String(value.entity.name.dropFirst(8)) }
                })
            } else if let failure {
                ContentUnavailableView("Can't show the scan", systemImage: "cube.transparent", description: Text(failure))
            } else {
                ProgressView("Loading the scan…")
            }
            if let id = actions.focusFindingId, let f = sync.snapshot?.findings.first(where: { $0.id == id }) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(f.subject).font(.subheadline.weight(.semibold))
                    Text("\(f.problem) · \(f.confidenceText)").font(.caption)
                    Text("Ask Lea about it, e.g. “what's that?”").font(.caption2).foregroundStyle(.secondary)
                }
                .padding().frame(maxWidth: .infinity, alignment: .leading).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 14)).padding()
            }
        }
        .navigationTitle("3D scan")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            guard let client = sync.client, let remote = sync.remoteId else { failure = "Not connected to the bridge."; return }
            do { model = try await client.meshView(sessionId: remote, reportId: reportId) } catch { failure = error.localizedDescription }
        }
    }

    /// Millimetres to metres, centred on the model, with per-vertex normals so the surface is shaded.
    private static func scene(_ m: MeshView) -> Entity {
        let root = Entity()
        let raw = Data(base64Encoded: m.positions) ?? Data(), idx = Data(base64Encoded: m.indices) ?? Data()
        let floats = raw.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) }
        let indices = idx.withUnsafeBytes { Array($0.bindMemory(to: UInt32.self)) }
        var points = stride(from: 0, to: floats.count - 2, by: 3).map { SIMD3(floats[$0], floats[$0 + 1], floats[$0 + 2]) }
        guard !points.isEmpty, indices.count >= 3 else { return root }
        let lo = points.reduce(points[0], pointwiseMin), hi = points.reduce(points[0], pointwiseMax)
        let centre = (lo + hi) / 2
        points = points.map { ($0 - centre) * 0.001 }
        var normals = [SIMD3<Float>](repeating: .zero, count: points.count)
        for t in stride(from: 0, to: indices.count - 2, by: 3) {
            let a = Int(indices[t]), b = Int(indices[t + 1]), c = Int(indices[t + 2])
            guard a < points.count, b < points.count, c < points.count else { continue }
            let n = cross(points[b] - points[a], points[c] - points[a])
            normals[a] += n; normals[b] += n; normals[c] += n
        }
        var descriptor = MeshDescriptor(name: "scan")
        descriptor.positions = MeshBuffers.Positions(points)
        descriptor.normals = MeshBuffers.Normals(normals.map { length($0) > 0 ? normalize($0) : SIMD3(0, 0, 1) })
        descriptor.primitives = .triangles(indices)
        if let mesh = try? MeshResource.generate(from: [descriptor]) {
            root.addChild(ModelEntity(mesh: mesh, materials: [SimpleMaterial(color: .lightGray, roughness: 0.8, isMetallic: false)]))
        }
        for r in m.regions where r.centroid.count == 3 && r.extent.count == 3 {
            let size = SIMD3(Float(max(r.extent[0], 3)), Float(max(r.extent[1], 3)), Float(max(r.extent[2], 3))) * 0.001
            let marker = ModelEntity(mesh: .generateBox(size: size),
                                     materials: [UnlitMaterial(color: (r.kind == "missing_material" ? UIColor.systemOrange : .systemYellow).withAlphaComponent(0.55))])
            marker.position = (SIMD3(Float(r.centroid[0]), Float(r.centroid[1]), Float(r.centroid[2])) - centre) * 0.001
            marker.name = "finding:\(r.findingId)"
            marker.components.set(CollisionComponent(shapes: [.generateBox(size: size)]))
            marker.components.set(InputTargetComponent())
            root.addChild(marker)
        }
        return root
    }
}
