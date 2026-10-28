import Foundation
import SwiftUI

/// Paid-service usage on the Mac (backend/repairos/spend.py): a banner when a service is paused or close to its daily
/// limit (the live event `spend_alert`, from GET /v1/events and every session's event stream), and the Settings
/// "Usage" screen (GET /v1/usage). Service names come from the backend (`label`), so the app holds no list of them.
@MainActor
final class UsageAlerts: ObservableObject {
    static let shared = UsageAlerts()

    struct Alert: Equatable, Identifiable {
        let id: Int
        let label: String
        let reason: String
        let until: Date?

        /// A trip pauses the service; a `near_*` alert only warns.
        var paused: Bool { !reason.hasPrefix("near_") }

        func words(now: Date = Date()) -> String {
            guard paused else { return "\(label) is close to today's usage limit" }
            let minutes = until.map { max(1, Int(($0.timeIntervalSince(now) / 60).rounded(.up))) }
            return "Usage limit reached for \(label)" + (minutes.map { "; paused for \($0) min" } ?? "")
        }

        init?(json: [String: Any], now: Date = Date()) {
            guard let id = json["alert_id"] as? Int, let reason = json["reason"] as? String else { return nil }
            self.id = id
            self.reason = reason
            label = (json["label"] as? String) ?? (json["service"] as? String) ?? "A service"
            if let s = json["retry_after_s"] as? Double, let at = (json["at"] as? String).flatMap(Self.date) {
                until = at.addingTimeInterval(s)
            } else if let s = json["retry_after_s"] as? Int, let at = (json["at"] as? String).flatMap(Self.date) {
                until = at.addingTimeInterval(Double(s))
            } else {
                until = nil
            }
        }

        static func date(_ text: String) -> Date? { ISO8601DateFormatter().date(from: text) }
    }

    /// The banner on screen, if any. Dismissed by a tap, or on its own after a few seconds.
    @Published private(set) var banner: Alert?
    private var seen: Set<Int> = []
    private var task: Task<Void, Never>?
    private var hide: Task<Void, Never>?

    /// One `spend_alert` payload (from any stream). Old ones (a pause already over) and repeats are dropped.
    func receive(_ json: [String: Any], now: Date = Date()) {
        guard let alert = Alert(json: json, now: now), !seen.contains(alert.id) else { return }
        seen.insert(alert.id)
        if let until = alert.until, until < now { return }
        if !alert.paused, let at = (json["at"] as? String).flatMap(Alert.date), now.timeIntervalSince(at) > 600 { return }
        banner = alert
        hide?.cancel()
        hide = Task { [weak self] in
            try? await Task.sleep(for: .seconds(8))
            guard !Task.isCancelled else { return }
            self?.dismiss()
        }
    }

    func dismiss() {
        banner = nil
        hide?.cancel()
    }

    /// Listens to GET /v1/events while the app runs (reconnects with backoff; quiet when unpaired or offline).
    func start(_ app: AppState) {
        guard task == nil else { return }
        task = Task { [weak self] in
            var backoff: Double = 2
            while !Task.isCancelled {
                guard let self else { return }
                if let gw = app.gateway, gw.token != nil {
                    do {
                        try await self.listen(gw.globalEventsRequest())
                        backoff = 2
                    } catch is CancellationError {
                        return
                    } catch {
                        backoff = min(60, backoff * 2)
                    }
                }
                try? await Task.sleep(for: .seconds(backoff))
            }
        }
    }

    private static let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 40       // the gateway sends a keepalive every 15 s
        c.timeoutIntervalForResource = 24 * 3600
        return URLSession(configuration: c)
    }()

    private func listen(_ request: URLRequest) async throws {
        let (bytes, response) = try await Self.session.bytes(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw GatewayError.message("events unavailable") }
        var parser = SSEParser()
        var line: [UInt8] = []
        for try await byte in bytes {
            if byte == 0x0A {
                if line.last == 0x0D { line.removeLast() }
                if let e = parser.feed(String(decoding: line, as: UTF8.self)), e.name == "spend_alert",
                   let obj = (try? JSONSerialization.jsonObject(with: Data(e.data.utf8))) as? [String: Any] {
                    receive(obj)
                }
                line.removeAll(keepingCapacity: true)
            } else {
                line.append(byte)
            }
        }
    }
}

/// The non-blocking banner at the top of every screen (taps pass through everywhere else).
struct UsageBanner: View {
    @ObservedObject private var alerts = UsageAlerts.shared
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack {
            if let a = alerts.banner {
                Button { alerts.dismiss() } label: {
                    // An opaque surface card with the caution icon and words; a hairline edge and no shadow.
                    HStack(spacing: Spacing.s) {
                        Image(systemName: a.paused ? "pause.circle" : "gauge.with.dots.needle.67percent")
                            .foregroundStyle(Palette.caution)
                        Text(a.words()).multilineTextAlignment(.leading).foregroundStyle(Palette.ink)
                    }
                    .font(.label)
                    .padding(.horizontal, Spacing.l)
                    .padding(.vertical, Spacing.s)
                    .frame(minHeight: 44)
                    .background(Palette.surface, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: Radius.card, style: .continuous).strokeBorder(Palette.hairline, lineWidth: Line.hairline))
                }
                .buttonStyle(.plain)
                .accessibilityHint("Dismisses this notice")
                .transition(.move(edge: .top).combined(with: .opacity))
                .padding(.horizontal, Spacing.gutter)
                .padding(.top, Spacing.xs)
            }
            Spacer(minLength: 0)
        }
        .animation(Motion.pick(Motion.standard, reduceMotion: reduceMotion), value: alerts.banner)
        .haptic(Haptic.warn, trigger: alerts.banner?.id)
    }
}

/// Settings > Usage: per service, today's calls and the estimated cost, from GET /v1/usage.
struct UsageView: View {
    @EnvironmentObject private var app: AppState
    @State private var rows: [Row] = []
    @State private var total: Double?
    @State private var error: String?

    struct Row: Identifiable {
        let id: String
        let label: String
        let calls: Int
        let cap: Int
        let usd: Double?
        let paused: Bool
    }

    var body: some View {
        Form {
            Group {
                if let error {
                    Section { Label(error, systemImage: "exclamationmark.circle").foregroundStyle(Palette.danger) }
                }
                Section {
                    ForEach(rows) { r in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(r.label)
                                Text("\(r.calls) of \(r.cap) calls").font(.footnote).foregroundStyle(Palette.secondary)
                            }
                            Spacer()
                            if r.paused { StatusPill(text: "Paused", symbol: "pause", tone: .caution) }
                            if let usd = r.usd { Text(usd, format: .currency(code: "USD")).monospacedDigit() }
                        }
                    }
                    if let total {
                        LabeledContent("Estimated cost") { Text(total, format: .currency(code: "USD")).monospacedDigit() }
                    }
                } header: { Text("Last 24 hours") } footer: {
                    Text("The lea server stops a service for a while when it reaches its limit, so a runaway can't keep spending.")
                }
            }
            .listRowBackground(Palette.surface)
        }
        .settingsList()
        .navigationTitle("Usage")
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        guard let gw = app.gateway else { error = "Connect to the lea server first."; return }
        do {
            let u = try await gw.usage()
            let services = u["services"] as? [String: [String: Any]] ?? [:]
            rows = services.keys.sorted().compactMap { key in
                guard let s = services[key] else { return nil }
                let caps = s["caps"] as? [String: Any] ?? [:]
                let breaker = s["breaker"] as? [String: Any] ?? [:]
                return Row(id: key, label: s["label"] as? String ?? key, calls: Int(s["last_day"] as? Double ?? 0),
                           cap: Int(caps["per_day"] as? Double ?? 0),
                           usd: (s["usd_tracked"] as? Bool ?? false) ? s["usd_day"] as? Double ?? 0 : nil,
                           paused: breaker["open"] as? Bool ?? false)
            }
            total = u["usd_day_total"] as? Double
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
