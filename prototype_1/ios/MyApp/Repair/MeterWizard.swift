import SwiftUI

/// The pending checks one at a time, in the order Jev ranked them (or creation order, said so, when it couldn't).
/// Each is spoken; a powered or unpowered state must be confirmed before a reading; the verdict is the module's.
struct MeterWizard: View {
    @EnvironmentObject private var sync: SessionSync
    @EnvironmentObject private var conversation: VoiceCoordinator
    @Environment(\.dismiss) private var dismiss
    @State private var order: BridgeClient.NextChecks?
    @State private var reading = ""
    @State private var confirmedPower = false

    private var check: RepairCheck? {
        order?.order.lazy.compactMap { id in sync.snapshot?.checks.first { $0.id == id && $0.outcome == "pending" } }.first
    }

    var body: some View {
        NavigationStack {
            Form {
                if let order { Text(order.ranked ? "Order chosen by Jev." : "Not ranked (\(order.reason ?? "Jev unavailable")); in the order they were added.").font(.caption) }
                if let check {
                    let finding = sync.snapshot?.findings.first { $0.id == check.findingId }
                    Section(finding.map { "\($0.subject): \($0.problem)" } ?? "Check") {
                        Text(check.instruction).font(.headline)
                        Label(check.safety, systemImage: "bolt.slash").foregroundStyle(.orange)
                        Toggle(check.powerState == "off" ? "It is unplugged and the batteries are out" : "Only a low-voltage supply is connected", isOn: $confirmedPower)
                        if !check.expectedUnit.isEmpty {
                            TextField("Reading (\(check.expectedUnit))", text: $reading).keyboardType(.numbersAndPunctuation).disabled(!confirmedPower)
                            Text("Expected \(check.expectedMin.map { $0.formatted() } ?? "–")…\(check.expectedMax.map { $0.formatted() } ?? "–") \(check.expectedUnit) (\(check.expectedSource))").font(.caption)
                        }
                    }
                    Section {
                        if !check.expectedUnit.isEmpty {
                            Button("Save reading") { record(check, observed: "as_expected") }.disabled(!confirmedPower || Double(reading) == nil)
                        } else {
                            Button("Looks as expected") { record(check, observed: "as_expected") }.disabled(!confirmedPower)
                            Button("Not as expected") { record(check, observed: "not_as_expected") }.disabled(!confirmedPower)
                            Text("Without a reading or a result photo this is recorded as unverified.").font(.caption)
                        }
                        Button("Couldn't check", role: .destructive) { record(check, observed: "could_not_check") }
                    }
                } else if order != nil {
                    Text("No checks left. Results are on the Repair screen.")
                }
            }
            .navigationTitle("Checks")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .task { await load() }
            .onChange(of: check?.id) {
                confirmedPower = false
                reading = ""
                if let check { conversation.sayAside("\(check.instruction). \(check.safety)") }
            }
        }
    }

    private func load() async {
        order = await sync.perform { c, r in try await c.nextChecks(sessionId: r) }
    }

    private func record(_ check: RepairCheck, observed: String) {
        let value = Double(reading)
        Task {
            await sync.sendOrKeep("POST", { "/v1/sessions/\($0)/checks/\(check.id)/outcome" },
                                  BridgeClient.OutcomeRequest(observed: observed, observationAssetId: nil, readingValue: value,
                                                              readingUnit: value == nil ? nil : check.expectedUnit, note: nil))
            await load()
        }
    }
}
