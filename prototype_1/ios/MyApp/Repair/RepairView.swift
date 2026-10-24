import ImageIO
import SwiftUI

/// Candidates from geometry and close-ups, each linked to its evidence and saved reference, with the
/// safe checks that confirm or rule them out, and the record of whether a repair worked.
struct RepairView: View {
    @EnvironmentObject private var store: CaptureStore
    @EnvironmentObject private var sync: SessionSync
    @State private var compareSubject = ""
    @State private var closeupId: UUID?
    @State private var referenceId: UUID?
    @State private var walking = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let report = sync.snapshot?.meshReports.max(by: { $0.createdAt < $1.createdAt }) {
                        NavigationLink { ModelView(reportId: report.id) } label: {
                            Label("View the 3D scan\(report.passed ? "" : " (failed its checks: visual only)")", systemImage: "cube.transparent").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.bordered)
                    }
                    compareCard
                    if sync.snapshot?.checks.contains(where: { $0.outcome == "pending" }) == true {
                        Button { walking = true } label: { Label("Walk through the checks", systemImage: "gauge.with.dots.needle.50percent").frame(maxWidth: .infinity) }
                            .buttonStyle(.borderedProminent).tint(.mint)
                    }
                    if let snap = sync.snapshot {
                        if snap.findings.isEmpty {
                            ContentUnavailableView("No candidates yet", systemImage: "magnifyingglass",
                                                   description: Text("A passing scan or a close-up comparison adds candidates here."))
                        }
                        ForEach(snap.findings.sorted { $0.createdAt > $1.createdAt }) { finding in
                            NavigationLink { FindingDetail(findingId: finding.id) } label: { FindingRow(finding: finding) }
                                .buttonStyle(.plain)
                        }
                    } else {
                        ContentUnavailableView("Not synced", systemImage: "icloud.slash", description: Text(sync.connection))
                    }
                }
                .padding()
            }
            .navigationTitle("Repair")
            .sheet(isPresented: $walking) { MeterWizard() }
        }
    }

    private var compareCard: some View {
        let photos = store.activeSession.photos
        let closeups = photos.filter { $0.effectiveRole == .closeup }
        let references = photos.filter { $0.effectiveRole == .reference }
        return VStack(alignment: .leading, spacing: 10) {
            Text("Compare a close-up").font(.headline)
            Text("Pick a close-up and the saved reference photo of the same area.").font(.caption).foregroundStyle(.secondary)
            Picker("Close-up", selection: $closeupId) {
                Text("Choose close-up").tag(UUID?.none)
                ForEach(closeups) { Text($0.subject ?? $0.capturedAt.formatted(date: .omitted, time: .standard)).tag(Optional($0.id)) }
            }
            Picker("Reference", selection: $referenceId) {
                Text("Choose reference").tag(UUID?.none)
                ForEach(references) { Text($0.subject ?? $0.capturedAt.formatted(date: .omitted, time: .standard)).tag(Optional($0.id)) }
            }
            TextField("Area (e.g. fan connector)", text: $compareSubject)
                .textFieldStyle(.roundedBorder)
            Button {
                guard let c = closeupId, let r = referenceId else { return }
                Task {
                    await sync.syncActiveSession()
                    _ = await sync.perform { client, remote in
                        try await client.compareCloseup(sessionId: remote, closeupAssetId: SessionSync.assetId(c), referencePhotoAssetId: SessionSync.assetId(r),
                                                        subject: compareSubject, requestId: "cmp-\(c.uuidString)-\(r.uuidString)")
                    }
                }
            } label: { Label("Compare", systemImage: "rectangle.on.rectangle").frame(maxWidth: .infinity) }
            .buttonStyle(.borderedProminent).tint(.mint)
            .disabled(closeupId == nil || referenceId == nil || sync.health?.vision.anthropicConfigured == false)
            if sync.health?.vision.anthropicConfigured == false {
                Text("The bridge has no Anthropic key, so close-up comparison is off.").font(.caption).foregroundStyle(.orange)
            }
            if let job = sync.snapshot?.jobs.filter({ $0.kind == "closeup_compare" }).max(by: { $0.createdAt < $1.createdAt }) {
                JobStatusLine(job: job)
            }
        }
        .padding(18)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 20))
    }
}

struct FindingRow: View {
    let finding: Finding

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon).foregroundStyle(color).font(.title3)
            VStack(alignment: .leading, spacing: 3) {
                Text(finding.subject).font(.subheadline.weight(.semibold))
                Text(finding.problem).font(.subheadline)
                Text("\(stateLabel) · \(finding.source) · \(finding.confidenceText)").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Image(systemName: "chevron.right").foregroundStyle(.tertiary)
        }
        .padding(14)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }

    private var stateLabel: String {
        switch finding.state {
        case "candidate": "Unverified candidate"
        case "confirmed": "Confirmed by check"
        case "ruled_out": "Ruled out by check"
        default: "Could not be verified"
        }
    }
    private var icon: String {
        switch finding.state {
        case "confirmed": "exclamationmark.triangle.fill"
        case "ruled_out": "checkmark.seal"
        case "candidate": "questionmark.diamond"
        default: "questionmark.circle"
        }
    }
    private var color: Color { finding.state == "confirmed" ? .red : finding.state == "ruled_out" ? .mint : .orange }
}

struct JobStatusLine: View {
    let job: RemoteJob
    var body: some View {
        HStack(spacing: 8) {
            if job.isTerminal { Image(systemName: job.status == "succeeded" ? "checkmark.circle.fill" : "exclamationmark.circle").foregroundStyle(job.status == "succeeded" ? .mint : .orange) }
            else { ProgressView() }
            VStack(alignment: .leading) {
                Text("\(job.kind.replacingOccurrences(of: "_", with: " ")): \(job.status.replacingOccurrences(of: "_", with: " "))").font(.caption.weight(.semibold))
                if !job.progress.isEmpty, !job.isTerminal { Text(job.progress).font(.caption2).foregroundStyle(.secondary) }
                if !job.error.isEmpty { Text(job.error).font(.caption2).foregroundStyle(.orange) }
            }
        }
    }
}

private struct FindingDetail: View {
    @EnvironmentObject private var store: CaptureStore
    @EnvironmentObject private var sync: SessionSync
    let findingId: String
    @State private var addingCheck = false
    @State private var outcomeFor: RepairCheck?
    @State private var repairAction = ""

    var body: some View {
        let snap = sync.snapshot
        let finding = snap?.findings.first { $0.id == findingId }
        let checks = snap?.checks.filter { $0.findingId == findingId } ?? []
        let repairs = snap?.repairs.filter { $0.findingId == findingId } ?? []
        ScrollView {
            if let finding {
                VStack(alignment: .leading, spacing: 16) {
                    FindingRow(finding: finding)
                    if !finding.detail.isEmpty { Text(finding.detail).font(.callout) }
                    Text("Evidence").font(.headline)
                    ScrollView(.horizontal) {
                        HStack {
                            ForEach(finding.evidenceAssetIds + [finding.referenceAssetId], id: \.self) { id in
                                EvidenceThumb(assetId: id, isReference: id == finding.referenceAssetId)
                            }
                        }
                    }
                    Text("Checks").font(.headline)
                    ForEach(checks) { check in
                        CheckCard(check: check) { outcomeFor = check }
                    }
                    Button { addingCheck = true } label: { Label("Add a measured check", systemImage: "gauge.with.dots.needle.33percent") }
                    Text("Repair").font(.headline)
                    ForEach(repairs) { r in
                        Label("\(r.action): \(r.result.replacingOccurrences(of: "_", with: " "))", systemImage: r.result == "fixed_verified" ? "checkmark.seal.fill" : "wrench")
                            .foregroundStyle(r.result == "fixed_verified" ? .mint : .primary)
                    }
                    TextField("What you did (e.g. reseated connector)", text: $repairAction).textFieldStyle(.roundedBorder)
                    Button("Record repair and add its verification check") {
                        Task {
                            let check = await sync.perform { client, remote in
                                try await client.addCheck(sessionId: remote, findingId: findingId, .init(
                                    purpose: "verify_repair", instruction: "Repeat the check that found the fault: \(checks.last?.instruction ?? "inspect the area again")",
                                    safety: checks.last?.safety ?? "Unplug the product and remove batteries first.", powerState: checks.last?.powerState ?? "off",
                                    expectedMin: checks.last?.expectedMin, expectedMax: checks.last?.expectedMax,
                                    expectedUnit: checks.last?.expectedUnit ?? "", expectedSource: checks.last?.expectedSource ?? "",
                                    net: checks.last?.net ?? ""))
                            }
                            guard let check else { return }
                            _ = await sync.perform { client, remote in try await client.recordRepair(sessionId: remote, findingId: findingId, action: repairAction, verifyCheckId: check.id) }
                            repairAction = ""
                        }
                    }
                    .disabled(repairAction.trimmingCharacters(in: .whitespaces).isEmpty)
                }
                .padding()
            }
        }
        .navigationTitle(finding?.subject ?? "Finding")
        .sheet(isPresented: $addingCheck) { AddCheckSheet(findingId: findingId) }
        .sheet(item: $outcomeFor) { OutcomeSheet(check: $0) }
    }
}

private struct CheckCard: View {
    let check: RepairCheck
    let record: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(check.purpose == "verify_repair" ? "Verify the repair" : "Diagnose").font(.caption.weight(.bold)).foregroundStyle(.mint)
            Text(check.instruction).font(.subheadline)
            Label(check.safety, systemImage: "bolt.slash").font(.caption).foregroundStyle(.orange)
            Text("Power: \(check.powerState.replacingOccurrences(of: "_", with: " "))").font(.caption)
            if check.expectedMin != nil || check.expectedMax != nil {
                Text("Expected \(range) \(check.expectedUnit) (\(check.expectedSource))").font(.caption).foregroundStyle(.secondary)
            }
            if check.outcome == "pending" {
                Button("Record result", action: record).buttonStyle(.bordered)
            } else {
                Text("Result: \(check.outcome.replacingOccurrences(of: "_", with: " "))\(check.readingValue.map { " · read \($0.formatted()) \(check.readingUnit)" } ?? "")")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(check.outcome == "unverified" ? .orange : .primary)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 14))
    }

    private var range: String {
        "\(check.expectedMin.map { $0.formatted() } ?? "–")…\(check.expectedMax.map { $0.formatted() } ?? "–")"
    }
}

private struct AddCheckSheet: View {
    @EnvironmentObject private var sync: SessionSync
    @Environment(\.dismiss) private var dismiss
    let findingId: String
    @State private var instruction = ""
    @State private var unit = "V"
    @State private var minText = ""
    @State private var maxText = ""
    @State private var source = ""
    @State private var power = "off"
    @State private var net = ""
    @State private var designNets: [GrainNets.Net] = []

    var body: some View {
        NavigationStack {
            Form {
                TextField("Instruction (probe points, what to look at)", text: $instruction, axis: .vertical)
                Picker("Measurement", selection: $unit) {
                    Text("Voltage (V)").tag("V")
                    Text("Resistance / continuity (Ω)").tag("ohm")
                    Text("Visual only").tag("")
                }
                if unit == "V" {
                    Picker("Power during check", selection: $power) {
                        Text("Off").tag("off")
                        Text("On, low voltage only").tag("on_low_voltage")
                    }
                }
                if unit == "V", !designNets.isEmpty {
                    Picker("Range from the Grain design", selection: $net) {
                        Text("None (type it)").tag("")
                        ForEach(designNets, id: \.self) { n in Text("\(n.net): \(n.min.formatted())–\(n.max.formatted()) V").tag(n.net) }
                    }
                    .onChange(of: net) {
                        guard let n = designNets.first(where: { $0.net == net }) else { return }
                        minText = n.min.formatted(); maxText = n.max.formatted(); source = n.source
                    }
                }
                if !unit.isEmpty {
                    TextField("Expected minimum", text: $minText).keyboardType(.decimalPad)
                    TextField("Expected maximum", text: $maxText).keyboardType(.decimalPad)
                    TextField("Where the range comes from (datasheet, label)", text: $source)
                }
                Section { Text("Continuity and resistance are always measured unpowered. Do not use this flow above low voltage.").font(.caption) }
            }
            .navigationTitle("New check")
            .task {
                // Quiet on failure: a design without electrical data simply offers no ranges.
                if let c = sync.client, let r = sync.remoteId, let nets = try? await c.grainElectrical(sessionId: r) { designNets = nets.nets }
            }
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Add") {
                        Task {
                            let powerState = unit == "ohm" || unit.isEmpty ? "off" : power
                            _ = await sync.perform { client, remote in
                                try await client.addCheck(sessionId: remote, findingId: findingId, .init(
                                    purpose: "diagnose", instruction: instruction,
                                    safety: powerState == "off" ? "Unplug the product and remove batteries first." : "Low-voltage supply only; keep fingers off exposed conductors.",
                                    powerState: powerState, expectedMin: Double(minText), expectedMax: Double(maxText), expectedUnit: unit, expectedSource: source,
                                    net: unit == "V" ? net : ""))
                            }
                            dismiss()
                        }
                    }
                    .disabled(instruction.isEmpty || (!unit.isEmpty && (minText.isEmpty && maxText.isEmpty || source.isEmpty)))
                }
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
    }
}

private struct OutcomeSheet: View {
    @EnvironmentObject private var store: CaptureStore
    @EnvironmentObject private var sync: SessionSync
    @Environment(\.dismiss) private var dismiss
    let check: RepairCheck
    @State private var observed = "as_expected"
    @State private var readingText = ""
    @State private var photoId: UUID?
    @State private var note = ""

    var body: some View {
        NavigationStack {
            Form {
                Text(check.instruction)
                if !check.expectedUnit.isEmpty {
                    TextField("Reading (\(check.expectedUnit))", text: $readingText).keyboardType(.numbersAndPunctuation)
                    Text("Lea compares the reading with the sourced range itself.").font(.caption).foregroundStyle(.secondary)
                }
                Picker("What you saw", selection: $observed) {
                    Text("As expected").tag("as_expected")
                    Text("Not as expected").tag("not_as_expected")
                    Text("Could not check").tag("could_not_check")
                }
                Picker("Photo of the result", selection: $photoId) {
                    Text("None").tag(UUID?.none)
                    ForEach(store.activeSession.photos.filter { $0.effectiveRole == .observation }) {
                        Text($0.capturedAt.formatted(date: .omitted, time: .standard)).tag(Optional($0.id))
                    }
                }
                TextField("Note", text: $note)
                Section { Text("Without a reading or a result photo, this check is recorded as unverified.").font(.caption) }
            }
            .navigationTitle("Result")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        Task {
                            await sync.syncActiveSession()
                            await sync.sendOrKeep("POST", { "/v1/sessions/\($0)/checks/\(check.id)/outcome" }, BridgeClient.OutcomeRequest(
                                observed: observed, observationAssetId: photoId.map(SessionSync.assetId),
                                readingValue: Double(readingText), readingUnit: readingText.isEmpty ? nil : check.expectedUnit, note: note))
                            dismiss()
                        }
                    }
                }
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
    }
}

private struct EvidenceThumb: View {
    @EnvironmentObject private var sync: SessionSync
    @EnvironmentObject private var store: CaptureStore
    let assetId: String
    let isReference: Bool
    @State private var image: UIImage?

    var body: some View {
        let asset = sync.snapshot?.assets.first { $0.id == assetId }
        VStack(alignment: .leading, spacing: 4) {
            Group {
                if let image { Image(uiImage: image).resizable().scaledToFill() }
                else { Image(systemName: asset?.mediaType.hasPrefix("image") == true ? "photo" : "cube.transparent").font(.largeTitle).foregroundStyle(.secondary) }
            }
            .frame(width: 130, height: 100).clipped().background(.black.opacity(0.3)).clipShape(RoundedRectangle(cornerRadius: 10))
            Text(isReference ? "Saved reference" : (asset?.kind.replacingOccurrences(of: "_", with: " ") ?? assetId)).font(.caption2)
            Text(String((asset?.sha256 ?? "").prefix(12))).font(.caption2.monospaced()).foregroundStyle(.tertiary)
        }
        .task {
            guard let asset, asset.mediaType.hasPrefix("image") else { return }
            if let cached = Thumbnails.cache.object(forKey: asset.sha256 as NSString) { image = cached; return }
            // The original is usually on this iPhone already; otherwise download it once.
            let local = store.activeSession.photos.first { SessionSync.assetId($0.id) == assetId }
                .map { store.photoURL($0, in: store.activeSession) }
            var data: Data?
            if let local { data = try? Data(contentsOf: local) }
            else if let client = sync.client, let remote = sync.remoteId { data = try? await client.assetData(sessionId: remote, assetId: assetId) }
            guard let data, let thumb = Thumbnails.make(data) else { return }
            Thumbnails.cache.setObject(thumb, forKey: asset.sha256 as NSString)
            image = thumb
        }
    }
}

/// Small, decoded-once thumbnails keyed by content hash, so evidence lists don't decode full 12 MP photos.
private enum Thumbnails {
    static let cache = NSCache<NSString, UIImage>()

    static func make(_ data: Data, maxPixels: Int = 400) -> UIImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let cg = CGImageSourceCreateThumbnailAtIndex(source, 0, [kCGImageSourceCreateThumbnailFromImageAlways: true,
                                                                        kCGImageSourceCreateThumbnailWithTransform: true,
                                                                        kCGImageSourceThumbnailMaxPixelSize: maxPixels] as CFDictionary) else { return nil }
        return UIImage(cgImage: cg)
    }
}
