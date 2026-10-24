import Foundation
import Testing
@testable import RepairOS

/// "Fix it": the report's `fix` and the live `fix_ready` payload (same fields) parse to at most three offers with a
/// usable link; a price shows only with its currency.
struct FixItTests {
    static var json: [String: Any] { [
        "part": "power cord", "searched_at": "2026-10-24T20:00:00Z", "guide_link": "https://guides.example/g",
        "options": [
            ["title": "Offer A", "price": 12.99, "currency": "USD", "seller": "Shop One", "link": "https://one.example/a"],
            ["title": "Offer B", "seller": "two.example", "link": "https://two.example/b"],
            ["title": "Bad link", "seller": "x", "link": "javascript:alert(1)"],
            ["title": "Offer C", "price": 8, "currency": "GBP", "seller": "Shop Three", "link": "https://three.example/c"],
        ],
    ] }

    @Test func parsesOffersAndKeepsOnlyWebLinks() throws {
        let fix = try #require(FixIt(json: Self.json))
        #expect(fix.part == "power cord" && fix.guide?.host == "guides.example")
        #expect(fix.options.map(\.title) == ["Offer A", "Offer B"])   // first three, the bad link dropped
        #expect(fix.options[0].priceWords?.contains("12.99") == true && fix.options[1].priceWords == nil)
    }

    @Test func noOffersMeansNoCard() {
        #expect(FixIt(json: ["part": "x", "options": []]) == nil)
        #expect(FixIt(json: nil) == nil)
    }

    @Test func roundTripsForTheCachedReport() throws {
        let fix = try #require(FixIt(json: Self.json))
        #expect(FixIt(json: fix.json) == fix)
        #expect(ReportModel(json: ["verdict": "potential_issues", "fix": fix.json]).fix == fix)
    }

    @Test @MainActor func liveFixReadyEventSetsTheFix() {
        let live = LiveEvents(scanID: "scan_a")
        var payload = Self.json
        payload["scan_id"] = "scan_a"
        let data = String(decoding: try! JSONSerialization.data(withJSONObject: payload), as: UTF8.self)
        live.handle(SSEParser.Event(name: "fix_ready", data: data, id: "7"))
        #expect(live.fix?.part == "power cord")
        payload["scan_id"] = "scan_other"
        let other = LiveEvents(scanID: "scan_a")
        other.handle(SSEParser.Event(name: "fix_ready", data: String(decoding: try! JSONSerialization.data(withJSONObject: payload), as: UTF8.self), id: "8"))
        #expect(other.fix == nil)
        let capped = LiveEvents(scanID: "scan_a")
        capped.handle(SSEParser.Event(name: "fix_ready", data: #"{"scan_id":"scan_a","status":"limited","options":[]}"#, id: "9"))
        #expect(capped.fix == nil && capped.fixEnded == "limited")
    }
}
