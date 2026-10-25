import SwiftUI

/// The scan's design in Grain (backend handoff: POST/GET /v1/scans/{id}/grain). The person says what to make; the
/// backend measures, sends the scan, photos and verified dimensions to a linked Grain project and starts the design.
/// This sheet shows the stage, relays Grain's question and lists every version Grain built with its fit check and a
/// STEP file to share. Every word about the design comes from the backend; the app only lays it out.
struct GrainDesign: Equatable {
    struct Revision: Equatable, Identifiable {
        var id: Int { n }
        var n: Int
        var buildOK: Bool
        var fit: String?          // ok, error, not_checked; nil when the capture asked for no fit
        var fitErrors: [String]
        var hasSTEP: Bool
        var summary: String?
    }

    var stage: String
    var objective: String
    var error: String?
    var why: String?
    var questions: [String]
    var revisions: [Revision]
    var dimensions: [String]

    init?(json: [String: Any]?) {
        guard let json, let stage = json["stage"] as? String else { return nil }
        self.stage = stage
        objective = json["objective"] as? String ?? ""
        error = json["error"] as? String
        let q = json["question"] as? [String: Any]
        why = q?["why"] as? String
        questions = (q?["questions"] as? [[String: Any]] ?? []).compactMap { $0["text"] as? String }
        revisions = (json["revisions"] as? [[String: Any]] ?? []).compactMap { r in
            guard let n = r["n"] as? Int else { return nil }
            let fit = r["fit"] as? [String: Any]
            return Revision(n: n, buildOK: r["build_ok"] as? Bool ?? false, fit: fit?["status"] as? String,
                            fitErrors: fit?["errors"] as? [String] ?? [], hasSTEP: r["has_step"] as? Bool ?? false,
                            summary: r["summary"] as? String)
        }
        dimensions = ((json["measurement"] as? [String: Any])?["dimensions"] as? [[String: Any]] ?? []).compactMap { d in
            guard let name = d["name"] as? String, let v = (d["value_mm"] as? NSNumber)?.doubleValue else { return nil }
            let u = (d["uncertainty_mm"] as? NSNumber)?.doubleValue ?? 0
            let ok = d["verified"] as? Bool ?? false
            return "\(name): \(v.formatted(.number.precision(.fractionLength(2)))) ± \(u.formatted(.number.precision(.fractionLength(2)))) mm" + (ok ? "" : " (not verified)")
        }
    }

    var working: Bool { ["queued", "measured", "project", "uploaded", "capture", "designing"].contains(stage) }

    var stageWords: String {
        switch stage {
        case "queued", "measured": "Measuring the scan…"
        case "project", "uploaded", "capture": "Sending the scan and photos to Grain…"
        case "designing": "Grain is designing…"
        case "asking": "Grain has a question."
        case "done": "Grain finished the design."
        case "stopped": "Grain stopped before finishing."
        default: "The design could not continue."
        }
    }
}

struct GrainSheet: View {
    let scanID: String
    let gateway: GatewayClient
    @Environment(\.dismiss) private var dismiss
    @State private var design: GrainDesign?
    @State private var loaded = false
    @State private var objective = ""
    @State private var answer = ""
    @State private var sending = false
    @State private var problem: String?
    @State private var files: [Int: URL] = [:]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.l) {
                    if !loaded {
                        ProgressView().frame(maxWidth: .infinity)
                    } else if let design, design.stage != "failed" || !design.revisions.isEmpty {
                        status(design)
                    } else {
                        start(failed: design)
                    }
                    if let problem {
                        Text(problem).font(.footnote).foregroundStyle(Palette.danger)
                    }
                }
                .padding(Spacing.l)
            }
            .background(Palette.canvas.ignoresSafeArea())
            .navigationTitle("Design in Grain")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .task { await follow() }
        }
    }

    private func start(failed: GrainDesign?) -> some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            Text("Say what Grain should make from this scan. Lea sends the scan, the clearest photos and any verified measurements; Grain designs an editable part and checks its fit.")
                .font(.subheadline).foregroundStyle(.secondary)
            if let failed, let error = failed.error {
                Text(error).font(.footnote).foregroundStyle(Palette.danger)
            }
            FieldWell(symbol: "cube.transparent") {
                TextField("What should Grain make?", text: $objective, axis: .vertical).lineLimit(1...4)
            }
            Button(failed == nil ? "Design it" : "Try again") { Task { await begin(failed?.objective) } }
                .buttonStyle(.primary)
                .disabled(sending || (failed == nil && objective.trimmingCharacters(in: .whitespaces).count < 3))
        }
    }

    @ViewBuilder
    private func status(_ d: GrainDesign) -> some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text(d.objective).font(.headline)
            HStack(spacing: Spacing.s) {
                if d.working { ProgressView().controlSize(.small) }
                Text(d.stageWords).font(.subheadline).foregroundStyle(.secondary)
            }
            if let error = d.error, d.stage == "failed" { Text(error).font(.footnote).foregroundStyle(Palette.danger) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
        if d.stage == "asking" {
            VStack(alignment: .leading, spacing: Spacing.m) {
                if let why = d.why, !why.isEmpty { Text(why).font(.footnote).foregroundStyle(.secondary) }
                ForEach(d.questions, id: \.self) { Text($0).font(.subheadline) }
                FieldWell(symbol: "text.bubble") {
                    TextField("Your answer", text: $answer, axis: .vertical).lineLimit(1...5)
                }
                Button("Send to Grain") { Task { await reply() } }
                    .buttonStyle(.primary)
                    .disabled(sending || answer.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .card(tone: .accent)
        }
        if !d.dimensions.isEmpty {
            VStack(alignment: .leading, spacing: Spacing.xs) {
                SectionHeader("Measured on the scan")
                ForEach(d.dimensions, id: \.self) { Text($0).font(.footnote.monospacedDigit()) }
            }
        }
        ForEach(d.revisions.reversed()) { r in revision(r) }
    }

    private func revision(_ r: GrainDesign.Revision) -> some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text("Version \(r.n)").font(.headline)
            Label(r.buildOK ? "Builds cleanly" : "The build has errors",
                  systemImage: r.buildOK ? "checkmark.circle" : "exclamationmark.triangle")
                .font(.subheadline)
            if let fit = r.fit {
                Label(fit == "ok" ? "Fits the measured holes" : fit == "error" ? "Does not fit yet" : "Fit not checked",
                      systemImage: fit == "ok" ? "checkmark.seal" : "ruler")
                    .font(.subheadline)
                ForEach(r.fitErrors, id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary) }
            }
            if let s = r.summary, !s.isEmpty { Text(s).font(.footnote).foregroundStyle(.secondary).lineLimit(6) }
            if r.hasSTEP {
                if let url = files[r.n] {
                    ShareLink(item: url) { Label("Share the STEP file", systemImage: "square.and.arrow.up") }
                } else {
                    Button("Get the STEP file") { Task { await download(r.n) } }.disabled(sending)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
    }

    // MARK: - Calls

    private func follow() async {
        while !Task.isCancelled {
            do {
                design = GrainDesign(json: try await gateway.grainDesign(scanID: scanID))
                problem = nil
            } catch {
                problem = error.localizedDescription
            }
            loaded = true
            try? await Task.sleep(for: .seconds(design?.working == true ? 4 : 15))
        }
    }

    private func begin(_ again: String?) async {
        sending = true
        defer { sending = false }
        do {
            design = GrainDesign(json: try await gateway.startGrainDesign(scanID: scanID, objective: again ?? objective))
            problem = nil
        } catch {
            problem = error.localizedDescription
        }
    }

    private func reply() async {
        sending = true
        defer { sending = false }
        do {
            design = GrainDesign(json: try await gateway.answerGrain(scanID: scanID, text: answer))
            answer = ""
            problem = nil
        } catch {
            problem = error.localizedDescription
        }
    }

    private func download(_ n: Int) async {
        sending = true
        defer { sending = false }
        do {
            let data = try await gateway.grainFile(scanID: scanID, revision: n, name: "model.step")
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(scanID)_v\(n).step")
            try data.write(to: url, options: .atomic)
            files[n] = url
        } catch {
            problem = error.localizedDescription
        }
    }
}
