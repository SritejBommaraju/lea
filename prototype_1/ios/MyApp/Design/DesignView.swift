import SwiftUI

/// Grain's live project state, the design requests Lea sent (automatic, typed or spoken), and every
/// saved version before and after each change.
struct DesignView: View {
    @EnvironmentObject private var sync: SessionSync
    @State private var command = ""
    @State private var linkId = ""
    @State private var answer = ""
    @State private var sizes: [SizeRow] = []

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if sync.snapshot?.session.grainProjectId.isEmpty ?? true { linkCard } else { liveCard; composer; commandsCard; versionsCard }
                }
                .padding()
            }
            .navigationTitle("Design")
            .onAppear { sync.watchGrain(true) }
            .onDisappear { sync.watchGrain(false) }
        }
    }

    private var linkCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Link a Grain project").font(.headline)
            Text("Design changes go through Grain's own chat. Lea saves the project before and after each one.").font(.caption).foregroundStyle(.secondary)
            TextField("Existing project id (12 hex)", text: $linkId).textFieldStyle(.roundedBorder).textInputAutocapitalization(.never)
            HStack {
                Button("Link") { Task { _ = await sync.perform { c, r in try await c.linkGrain(sessionId: r, projectId: linkId, createName: nil) } } }
                    .disabled(linkId.count != 12)
                Button("Create new") { Task { _ = await sync.perform { c, r in try await c.linkGrain(sessionId: r, projectId: nil, createName: "Lea repair \(Date().formatted(date: .abbreviated, time: .omitted))") } } }
            }
            .buttonStyle(.bordered)
        }
        .padding(18).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 20))
    }

    private var liveCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Grain").font(.headline)
                Spacer()
                if let p = sync.grain?.project {
                    Label(p.busy ? "Working" : p.waiting ? "Waiting for an answer" : "Idle", systemImage: p.busy ? "gearshape.2" : p.waiting ? "questionmark.bubble" : "pause.circle")
                        .font(.caption.weight(.semibold)).foregroundStyle(p.busy ? .mint : p.waiting ? .orange : .secondary)
                }
            }
            if let p = sync.grain?.project {
                Text("\(p.name) · \(p.id) · \(p.turns) turns").font(.caption).foregroundStyle(.secondary)
                if let b = p.last_build { Text("Last build: \(b.ok ? "ok" : "\(b.errors) errors")").font(.caption) }
            } else if sync.health?.grain.reachable == false {
                Text("Grain is unreachable from the bridge.").font(.caption).foregroundStyle(.orange)
            }
            if sync.snapshot?.session.referenceMeshAssetId.isEmpty == true, sync.grain?.project?.last_build?.ok == true {
                Button("Use this Grain design as the reference") {
                    Task { _ = await sync.perform { c, r in try await c.referenceFromGrain(sessionId: r) } }
                }
                .font(.caption)
            }
            if sync.health?.grain.agentReady == false {
                Text("Grain's design agent has no model key configured, so chat turns will be refused.").font(.caption).foregroundStyle(.orange)
            }
            if let q = sync.grain?.question {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Grain asks").font(.caption.weight(.bold))
                    if let why = q.why { Text(why).font(.caption) }
                    ForEach(q.questions, id: \.text) { item in
                        Text("• \(item.text)\(item.options.map { " (\($0.joined(separator: " / ")))" } ?? "")").font(.caption)
                    }
                    ForEach(q.questions.flatMap { $0.options ?? [] }, id: \.self) { option in
                        Button(option) { send("Answer to your question: \(option)") }.buttonStyle(.bordered).font(.caption)
                    }
                    HStack {
                        TextField("Your answer", text: $answer).textFieldStyle(.roundedBorder)
                        Button("Answer") { send("Answer to your question: \(answer)"); answer = "" }.disabled(answer.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
                .padding(10).background(.orange.opacity(0.15), in: RoundedRectangle(cornerRadius: 10))
            }
            ForEach(Array((sync.grain?.messages ?? []).suffix(6).enumerated()), id: \.offset) { _, m in
                if let text = m.text, !text.isEmpty {
                    Text("\(m.role == "user" ? "You" : "Grain"): \(text)").font(.caption).lineLimit(4)
                }
            }
        }
        .padding(18).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 20))
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("Design change, e.g. increase the mounting hole diameter by 2 mm", text: $command, axis: .vertical)
                .textFieldStyle(.roundedBorder)
            DisclosureGroup("Sizes to verify on the returned STEP (\(sizes.count))") {
                ForEach($sizes) { $size in
                    HStack {
                        Picker("Side", selection: $size.check.axis) { ForEach(["longest", "middle", "shortest"], id: \.self) { Text($0) } }
                        TextField("mm", value: $size.check.mm, format: .number).keyboardType(.decimalPad).textFieldStyle(.roundedBorder)
                        TextField("± mm", value: $size.check.tolMm, format: .number).keyboardType(.decimalPad).textFieldStyle(.roundedBorder)
                    }
                }
                if sizes.count < 3 { Button("Add a size") { sizes.append(SizeRow()) } }
            }
            .font(.caption)
            Button {
                send(command, checks: sizes.map(\.check).filter { $0.mm > 0 && $0.tolMm > 0 }) { command = ""; sizes = [] }
            } label: { Label("Send to Grain", systemImage: "paperplane.fill").frame(maxWidth: .infinity) }
            .buttonStyle(.borderedProminent).tint(.mint)
            .disabled(command.trimmingCharacters(in: .whitespaces).isEmpty)
        }
    }

    private var commandsCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Design requests").font(.headline)
            ForEach((sync.snapshot?.designCommands ?? []).sorted { $0.createdAt > $1.createdAt }) { cmd in
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(cmd.origin.capitalized) · \(cmd.status.replacingOccurrences(of: "_", with: " "))").font(.caption.weight(.bold))
                        .foregroundStyle(cmd.status == "succeeded" ? .mint : ["failed", "uncertain"].contains(cmd.status) ? .orange : .secondary)
                    Text(cmd.text).font(.caption).lineLimit(5)
                    if let job = sync.snapshot?.jobs.first(where: { $0.id == cmd.jobId }) { JobStatusLine(job: job) }
                    if cmd.status == "uncertain" {
                        // The turn may have reached Grain; its chat is shown above. Send again only if it isn't there.
                        Button("Not in Grain's chat? Send again") { send(cmd.text) }.font(.caption)
                    }
                }
                Divider()
            }
        }
        .padding(18).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 20))
    }

    private func send(_ text: String, checks: [BridgeClient.DimensionCheck] = [], then done: @escaping () -> Void = {}) {
        Task {
            if await sync.perform({ c, r in try await c.submitDesign(sessionId: r, text: text, origin: "text", meshReportId: nil,
                                                                  requestId: "txt-\(UUID().uuidString)", checks: checks) }) != nil { done() }
        }
    }

    private var versionsCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Saved versions").font(.headline)
            ForEach((sync.snapshot?.designVersions ?? []).sorted { $0.createdAt > $1.createdAt }) { v in
                NavigationLink { VersionDetail(version: v) } label: {
                    HStack {
                        Image(systemName: v.id == sync.snapshot?.session.activeDesignVersionId ? "star.fill" : "clock.arrow.circlepath")
                        VStack(alignment: .leading) {
                            Text("\(v.phase == "pre" ? "Before" : v.phase == "post" ? "After" : "Baseline") · \(v.fileCount) files").font(.subheadline)
                            Text(v.createdAt.formatted(date: .abbreviated, time: .standard)).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if let ok = v.buildOk { Text(ok ? "built" : "not built").font(.caption).foregroundStyle(ok ? .mint : .orange) }
                    }
                }
            }
        }
        .padding(18).background(.thinMaterial, in: RoundedRectangle(cornerRadius: 20))
    }
}

/// One saved version: its file list, the STEP re-import result, and any file opened from Lea's copy.
private struct VersionDetail: View {
    @EnvironmentObject private var sync: SessionSync
    let version: DesignVersion
    @State private var manifest: DesignSnapshotManifest?
    @State private var shareURL: URL?

    var body: some View {
        List {
            if let step = manifest?.stepCheck {
                Section("STEP re-import") {
                    Text(step.ok ? "Valid solid" : "Failed: \(step.error ?? "not a valid solid")").foregroundStyle(step.ok ? .mint : .orange)
                    if let bb = step.bbox_mm { Text("Box \(bb.map { $0.formatted(.number.precision(.fractionLength(1))) }.joined(separator: " × ")) mm") }
                    if let n = step.solids { Text("\(n) solid(s)") }
                    ForEach(step.dimension_checks ?? [], id: \.self) { d in
                        Label("\(d.dimension): \(d.measured_mm.formatted()) mm (asked \(d.expected_mm.formatted()) ± \(d.tolerance_mm.formatted()))",
                              systemImage: d.ok ? "checkmark" : "xmark").foregroundStyle(d.ok ? .mint : .orange)
                    }
                }
            }
            Section("Files (Lea's copy, \(manifest?.takenAt ?? ""))") {
                ForEach(manifest?.files ?? []) { f in
                    Button { Task { await open(f) } } label: {
                        VStack(alignment: .leading) {
                            Text(f.path).font(.caption.monospaced())
                            Text("\(f.size) bytes · \(f.sha256.prefix(12))").font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .navigationTitle(version.phase == "pre" ? "Before change" : version.phase == "post" ? "After change" : "Baseline")
        .task {
            guard let client = sync.client, let remote = sync.remoteId,
                  let data = try? await client.assetData(sessionId: remote, assetId: version.manifestAssetId) else { return }
            manifest = try? JSONDecoder().decode(DesignSnapshotManifest.self, from: data)
        }
        .sheet(item: Binding(get: { shareURL.map(IdentifiedURL.init) }, set: { shareURL = $0?.url })) { ShareSheet(url: $0.url) }
    }

    private func open(_ f: DesignSnapshotManifest.Entry) async {
        guard let client = sync.client, let remote = sync.remoteId,
              let data = try? await client.assetData(sessionId: remote, assetId: f.assetId) else { return }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent((f.path as NSString).lastPathComponent)
        try? data.write(to: url)
        shareURL = url
    }
}

private struct SizeRow: Identifiable {
    let id = UUID()
    var check = BridgeClient.DimensionCheck(axis: "longest", mm: 0, tolMm: 0.5)
}

struct IdentifiedURL: Identifiable { let url: URL; var id: String { url.path } }

struct ShareSheet: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> UIActivityViewController { UIActivityViewController(activityItems: [url], applicationActivities: nil) }
    func updateUIViewController(_ vc: UIActivityViewController, context: Context) {}
}
