import Foundation
import Testing
@testable import RepairOS

/// The `spend_alert` banner: a pause names the service (the backend's own label) and the minutes left; repeats and
/// pauses already over are not shown.
@MainActor
struct UsageAlertsTests {
    static let now = ISO8601DateFormatter().date(from: "2026-10-24T20:00:00Z")!

    static func alert(id: Int, reason: String = "per_minute", at: String = "2026-10-24T19:59:00Z",
                      retry: Int? = 300) -> [String: Any] {
        var a: [String: Any] = ["alert_id": id, "service": "anthropic", "label": "Claude", "reason": reason,
                                "used": 400, "cap": 400, "at": at]
        if let retry { a["retry_after_s"] = retry }
        return a
    }

    @Test func aPauseNamesTheServiceAndTheMinutesLeft() throws {
        let a = try #require(UsageAlerts.Alert(json: Self.alert(id: 1), now: Self.now))
        #expect(a.paused)
        #expect(a.words(now: Self.now) == "Usage limit reached for Claude; paused for 4 min")
        let near = try #require(UsageAlerts.Alert(json: Self.alert(id: 2, reason: "near_per_day", retry: nil)))
        #expect(!near.paused && near.words() == "Claude is close to today's usage limit")
    }

    @Test func repeatsAndFinishedPausesAreDropped() {
        let center = UsageAlerts()
        center.receive(Self.alert(id: 7, at: "2026-10-24T19:00:00Z"), now: Self.now)   // over an hour ago
        #expect(center.banner == nil)
        center.receive(Self.alert(id: 8), now: Self.now)
        #expect(center.banner?.id == 8)
        center.dismiss()
        center.receive(Self.alert(id: 8), now: Self.now)
        #expect(center.banner == nil)
    }
}
