import SwiftUI

/// Pair with the bridge on the laptop: its LAN URL (printed when it starts) and a token from `npm run pair`.
struct BridgeSettingsView: View {
    @EnvironmentObject private var sync: SessionSync
    @Environment(\.dismiss) private var dismiss
    @State private var url = BridgeSettings.baseURL?.absoluteString ?? "http://"
    @State private var token = BridgeSettings.token ?? ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Bridge") {
                    TextField("http://192.168.x.x:8787", text: $url).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("Device token (lea_…)", text: $token).textInputAutocapitalization(.never)
                }
                if let h = sync.health {
                    Section("Status") {
                        status("SpacetimeDB", h.spacetimedb.connected)
                        status("Grain", h.grain.reachable, detail: h.grain.agentReady == false ? "reachable, design agent not configured" : nil)
                        status("Cartesia voice", h.voice.cartesiaConfigured)
                        status("Close-up comparison", h.vision.anthropicConfigured)
                    }
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        BridgeSettings.baseURL = URL(string: url.trimmingCharacters(in: .whitespaces))
                        BridgeSettings.token = token.trimmingCharacters(in: .whitespaces)
                        sync.reconnect()
                        dismiss()
                    }
                }
            }
        }
    }

    private func status(_ name: String, _ ok: Bool, detail: String? = nil) -> some View {
        LabeledContent(name) { Text(detail ?? (ok ? "ready" : "not configured")).foregroundStyle(ok && detail == nil ? .mint : .orange) }
    }
}
