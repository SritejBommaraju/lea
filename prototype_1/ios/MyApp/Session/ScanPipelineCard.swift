import SwiftUI

/// Bridge status, the owner's frozen measurement policy, reconstruction, and the mesh QA verdict.
/// A mesh that fails any check is shown as visual context only and drives neither diagnosis nor CAD.
struct ScanPipelineCard: View {
    @EnvironmentObject private var store: CaptureStore
    @EnvironmentObject private var sync: SessionSync
    @State private var editingPolicy = false
    @State private var objective = ""

    var body: some View {
        let snap = sync.snapshot
        let orbit = store.activeSession.photos.filter { $0.effectiveRole == .orbit }
        let policy = snap?.policies.last
        let report = snap?.meshReports.max { $0.createdAt < $1.createdAt }
        let recon = snap?.jobs.filter { $0.kind == "reconstruction" }.max { $0.createdAt < $1.createdAt }

        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Pipeline").font(.headline)
                Spacer()
                Label(sync.connection, systemImage: sync.connection == "Connected" ? "bolt.horizontal.circle.fill" : "bolt.horizontal.circle")
                    .font(.caption).foregroundStyle(sync.connection == "Connected" ? .mint : .orange)
            }
            row("Photo evidence", "\(store.activeSession.photos.count) on this iPhone\(sync.pendingUploads > 0 ? ", \(sync.pendingUploads) uploading" : "")", ok: true)
            row("Saved reference", snap?.session.referenceMeshAssetId.isEmpty == false ? "Pinned for this session" : "Import a reference mesh in Evidence", ok: snap?.session.referenceMeshAssetId.isEmpty == false)
            if let policy {
                row("Measured scale", "\(policy.scaleAnchorLabel) side = \(policy.scaleAnchorMm.formatted()) mm; check: \(policy.holdoutLabel) = \(policy.holdoutMm.formatted()) mm ± \(policy.maxHoldoutErrorMm.formatted())", ok: true)
            } else {
                Button { editingPolicy = true } label: { Label("Enter two caliper measurements", systemImage: "ruler") }
            }
            if let recon { JobStatusLine(job: recon) }
            if let report { ReportView(report: report) }

            TextField("Bracket objective for automatic CAD (optional)", text: $objective, axis: .vertical)
                .textFieldStyle(.roundedBorder).font(.caption)
            Button {
                guard let policy else { return }
                Task {
                    await sync.syncActiveSession()
                    let units = store.activeSession.meshReferences.first?.unitsPerMm ?? 1
                    let ids = orbit.map { SessionSync.assetId($0.id) }
                    _ = await sync.perform { client, remote in
                        try await client.startReconstruction(sessionId: remote, .init(
                            photoAssetIds: ids, policyId: policy.id, referenceUnitsPerMm: units,
                            autoDesignObjective: objective.isEmpty ? nil : objective, meshAssetId: nil,
                            requestId: "recon-\(policy.id)-\(ids.count)-\(ids.last ?? "")"))
                    }
                }
            } label: { Label("Reconstruct \(orbit.count) orbit photos", systemImage: "cube.transparent").frame(maxWidth: .infinity) }
            .buttonStyle(.borderedProminent).tint(.mint)
            .disabled(policy == nil || orbit.count < 20 || recon.map { !$0.isTerminal } == true)
            if orbit.count < 20 { Text("Reconstruction needs at least 20 overlapping orbit photos.").font(.caption).foregroundStyle(.secondary) }
            if let scan = store.activeSession.scans?.last(where: { $0.objFilename != nil }) {
                // A model the phone already built: measured by the same QA, never rebuilt.
                Button {
                    guard let policy else { return }
                    Task {
                        await sync.syncActiveSession()
                        let ids = scan.photoIds.map(SessionSync.assetId)
                        _ = await sync.perform { client, remote in
                            try await client.startReconstruction(sessionId: remote, .init(
                                photoAssetIds: ids, policyId: policy.id, referenceUnitsPerMm: store.activeSession.meshReferences.first?.unitsPerMm ?? 1,
                                autoDesignObjective: objective.isEmpty ? nil : objective, meshAssetId: SessionSync.scanAssetId("obj-\(scan.id.uuidString)"),
                                requestId: "scan-\(policy.id)-\(scan.id.uuidString)"))
                        }
                    }
                } label: { Label("Measure the 3D scan (\(scan.photoIds.count) photos)", systemImage: "ruler").frame(maxWidth: .infinity) }
                .buttonStyle(.bordered)
                .disabled(policy == nil || recon.map { !$0.isTerminal } == true)
            }
        }
        .padding(18)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 20))
        .sheet(isPresented: $editingPolicy) { PolicySheet() }
    }

    private func row(_ title: String, _ detail: String, ok: Bool) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: ok ? "checkmark.circle.fill" : "clock").foregroundStyle(ok ? .mint : .orange)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.subheadline.weight(.semibold))
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

private struct ReportView: View {
    let report: MeshReport

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(report.passed ? "Mesh validated: may drive diagnosis and CAD" : "Mesh failed QA: visual context only",
                  systemImage: report.passed ? "checkmark.seal.fill" : "xmark.seal")
                .font(.subheadline.weight(.semibold)).foregroundStyle(report.passed ? .mint : .orange)
            // The module stores an untrusted or unmeasurable value as -1.
            check("Scale", !report.failedChecks.contains("scale"), report.scaleFactor > 0 ? "\(report.scaleFactor.formatted(.number.precision(.significantDigits(4)))) mm per unit" : "not trusted: the measured side can't be told apart from another")
            check("Holdout", !report.failedChecks.contains("holdout"), report.measuredHoldoutMm >= 0 ? "measured \(report.measuredHoldoutMm.formatted(.number.precision(.fractionLength(2)))) mm, error \(report.holdoutErrorMm.formatted(.number.precision(.fractionLength(2)))) mm" : "not trusted: the measured side can't be told apart from another")
            check("Coverage", !report.failedChecks.contains("coverage"), "\((report.coverage * 100).formatted(.number.precision(.fractionLength(1))))% of reference surface")
            check("Reference fit", !report.failedChecks.contains { $0.hasPrefix("fit") || $0.hasPrefix("reference") },
                  report.referenceMeshAssetId.isEmpty ? "no reference" : "RMS \(report.fitRmsMm.formatted(.number.precision(.fractionLength(2)))) mm, \((report.fitInlierShare * 100).formatted(.number.precision(.fractionLength(1))))% inliers")
            Text(report.engine).font(.caption2).foregroundStyle(.tertiary)
        }
    }

    private func check(_ name: String, _ ok: Bool, _ detail: String) -> some View {
        Label("\(name): \(detail)", systemImage: ok ? "checkmark" : "xmark").font(.caption).foregroundStyle(ok ? Color.primary : Color.orange)
    }
}

/// The owner's numbers (handoff O9). Lea has no defaults: every limit here is typed by a person.
private struct PolicySheet: View {
    @EnvironmentObject private var sync: SessionSync
    @Environment(\.dismiss) private var dismiss
    @State private var anchorAxis = "longest"
    @State private var anchorMm = ""
    @State private var holdoutAxis = "middle"
    @State private var holdoutMm = ""
    @State private var maxHoldoutError = ""
    @State private var minCoverage = ""
    @State private var maxFitRms = ""
    @State private var minInliers = ""
    private let axes = ["longest", "middle", "shortest"]

    var body: some View {
        NavigationStack {
            Form {
                Section("Scale anchor (sets scale)") {
                    Picker("Side of the object's box", selection: $anchorAxis) { ForEach(axes, id: \.self) { Text($0) } }
                    TextField("Measured length, mm", text: $anchorMm).keyboardType(.decimalPad)
                }
                Section("Holdout (tests scale; a different side)") {
                    Picker("Side", selection: $holdoutAxis) { ForEach(axes, id: \.self) { Text($0) } }
                    TextField("Measured length, mm", text: $holdoutMm).keyboardType(.decimalPad)
                    TextField("Allowed error, mm", text: $maxHoldoutError).keyboardType(.decimalPad)
                }
                Section("Coverage and fit against the saved reference") {
                    TextField("Minimum coverage (0–1)", text: $minCoverage).keyboardType(.decimalPad)
                    TextField("Maximum fit RMS, mm", text: $maxFitRms).keyboardType(.decimalPad)
                    TextField("Minimum inlier share (0–1)", text: $minInliers).keyboardType(.decimalPad)
                }
                Section { Text("These limits are frozen once saved. To change them, save a new policy.").font(.caption) }
            }
            .navigationTitle("Measurement policy")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Freeze") {
                        guard let a = Double(anchorMm), let h = Double(holdoutMm), let e = Double(maxHoldoutError),
                              let c = Double(minCoverage), let r = Double(maxFitRms), let i = Double(minInliers) else { return }
                        Task {
                            _ = await sync.perform { client, remote in
                                try await client.freezePolicy(sessionId: remote, .init(scaleAnchorAxis: anchorAxis, scaleAnchorMm: a, holdoutAxis: holdoutAxis, holdoutMm: h,
                                                                                       maxHoldoutErrorMm: e, minCoverage: c, maxFitRmsMm: r, minFitInlierShare: i))
                            }
                            dismiss()
                        }
                    }
                    .disabled(anchorAxis == holdoutAxis || [anchorMm, holdoutMm, maxHoldoutError, minCoverage, maxFitRms, minInliers].contains { Double($0) == nil })
                }
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
    }
}
