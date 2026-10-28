import SwiftUI

/// The design engine's reply to a request to change the product's design (POST /v1/scans/{id}/fix).
struct FixReply: Equatable, Sendable {
    var reply: String
    var finished: Bool
    /// The agent asked the customer something and is waiting for the answer.
    var waiting: Bool
    var errors: [String]
    /// `ok` (it rebuilt the design and every check passed), `failed`, or `none` (it did not run a build).
    var build: String

    init?(json: [String: Any]) {
        guard let reply = json["reply"] as? String else { return nil }
        self.reply = reply
        finished = json["finished"] as? Bool ?? false
        waiting = json["waiting"] as? Bool ?? false
        errors = json["errors"] as? [String] ?? []
        build = json["build"] as? String ?? "none"
    }

    /// One line about the build, in plain words; nil when none ran.
    var buildWords: String? {
        switch build {
        case "ok": "The design was rebuilt and every check passed."
        case "failed": "The design was rebuilt but a check still fails."
        default: nil
        }
    }
}

/// "Fix it with Grain": the customer says what should change (pre-filled with the symptom they reported), Grain's agent
/// changes the product's design and answers. The server adds the context the customer cannot type: the flagged parts,
/// the meter readings and their verdicts, the fixes suggested so far. The sheet lists that context in plain words, so
/// the customer sees what Grain will know before they send, and shows the reply when it arrives.
struct FixWithGrainSheet: View {
    let scanID: String
    /// What Grain is told besides the customer's words, one plain line each (CustomerWords.fixContext).
    let context: [String]
    let gateway: GatewayClient?
    /// Called when the design was rebuilt and every check passed (its picture has changed).
    var onBuilt: () -> Void = {}
    @Environment(\.dismiss) private var dismiss
    @State private var message: String
    @State private var sending = false
    @State private var reply: FixReply?
    @State private var error: String?

    init(scanID: String, symptom: String?, context: [String] = [], gateway: GatewayClient?, onBuilt: @escaping () -> Void = {}) {
        self.onBuilt = onBuilt
        self.scanID = scanID
        self.context = context
        self.gateway = gateway
        _message = SwiftUI.State(initialValue: symptom ?? "")
    }

    private var canSend: Bool {
        !sending && gateway != nil && !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextEditor(text: $message).frame(minHeight: 120)
                } header: { Text("What should change?") } footer: {
                    Text("Grain changes the product's design. It can take a few minutes.")
                }
                if !context.isEmpty {
                    Section {
                        ForEach(context.indices, id: \.self) { i in
                            Label(context[i], systemImage: "checkmark.circle").font(.subheadline)
                                .labelStyle(.titleAndIcon)
                        }
                    } header: { Text("Grain will also be told") } footer: {
                        Text("This comes from your report. It is sent with your words so you do not have to explain it again.")
                    }
                }
                if sending {
                    Section {
                        HStack(spacing: Spacing.m) {
                            ProgressView()
                            Text("Grain is working on it. This can take a few minutes; you can leave this open.")
                                .foregroundStyle(Palette.secondary)
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
                if let reply {
                    Section("Grain's reply") {
                        Text(reply.reply.isEmpty ? "Grain did not say anything." : reply.reply)
                        if let words = reply.buildWords { Text(words).font(.footnote).foregroundStyle(Palette.secondary) }
                        if reply.waiting {
                            Text("Grain is waiting for your answer. Reply above and send again.")
                                .font(.footnote).foregroundStyle(Palette.secondary)
                        }
                        ForEach(reply.errors.indices, id: \.self) { Text(reply.errors[$0]).font(.footnote).foregroundStyle(Palette.danger) }
                    }
                }
                if let error {
                    Section {
                        Text(error).foregroundStyle(Palette.danger)
                        Button("Try again") { Task { await send() } }.disabled(!canSend)
                    }
                }
            }
            .navigationTitle("Fix it with Grain")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Send") { Task { await send() } }.fontWeight(.semibold).disabled(!canSend)
                }
            }
        }
        .tint(Palette.accent)
    }

    private func send() async {
        guard let gateway else { return }
        sending = true
        error = nil
        defer { sending = false }
        do {
            let answer = try await gateway.fix(scanID: scanID, message: message.trimmingCharacters(in: .whitespacesAndNewlines))
            reply = answer
            if answer.build == "ok" { onBuilt() }
        } catch {
            self.error = error.localizedDescription
        }
    }
}
