import Foundation
import Testing
@testable import RepairOS

/// The product code the customer scans or types (ios/README.md, "Product code"), and the plain words for failures.
struct ProductCodeTests {
    @Test func qrFormReadsServerAndProduct() throws {
        let code = try #require(ProductCode.parse("repairos://g/lea.example.com/lamp-v2"))
        #expect(code.gateway == "https://lea.example.com")
        #expect(code.product == "lamp-v2")
        #expect(code.pairing == nil)
    }

    @Test func qrFormCarriesAPairingCode() throws {
        let code = try #require(ProductCode.parse("  repairos://g/192.168.1.10:8000/live-lamp-v2?pair=abc%20123\n"))
        #expect(code.gateway == "http://192.168.1.10:8000")   // a private address is reached over http
        #expect(code.product == "live-lamp-v2")
        #expect(code.pairing == "abc 123")
    }

    @Test func shortFormReadsTheSame() throws {
        let code = try #require(ProductCode.parse("lamp-v2@Lea.Example.com"))
        #expect(code.gateway == "https://lea.example.com")
        #expect(code.product == "lamp-v2")
    }

    @Test(arguments: ["", "   ", "lamp-v2", "lamp v2@lea.example.com", "@lea.example.com", "lamp@",
                      "repairos://g/lea.example.com", "repairos://g/lea.example.com/a/b", "repairos://x/lea.example.com/lamp",
                      "lamp@bad host", "lamp@-bad.example.com", "https://lea.example.com/lamp"])
    func notACode(_ text: String) {
        #expect(ProductCode.parse(text) == nil)
    }

    @Test func localHostsUseHttpAndOthersHttps() {
        for host in ["localhost:8000", "mac-mini", "lea.local", "10.0.0.5", "172.16.4.2:8000", "172.31.255.1", "192.168.0.9", "127.0.0.1"] {
            #expect(ProductCode.isLocal(host), "\(host)")
        }
        for host in ["lea.example.com", "172.32.0.1", "8.8.8.8", "192.169.0.1"] {
            #expect(!ProductCode.isLocal(host), "\(host)")
        }
    }

    @Test func theSameServerIsRecognisedWhateverTheSpelling() {
        #expect(AppState.sameServer("192.168.1.10:8000", "http://192.168.1.10:8000/"))
        #expect(AppState.sameServer("HTTPS://Lea.Example.com", "https://lea.example.com"))
        #expect(!AppState.sameServer("https://a.example.com", "https://b.example.com"))
    }

    @Test func codeSurvivesTheKeychainEncodingWithoutItsPairingCode() throws {
        let code = ProductCode(gateway: "https://lea.example.com", product: "lamp-v2", pairing: "secret")
        let back = try JSONDecoder().decode(ProductCode.self, from: JSONEncoder().encode(code))
        #expect(back.gateway == code.gateway && back.product == code.product)
        #expect(back.pairing == nil)
    }
}

struct FailureWordsTests {
    @Test func networkErrorsSayWhatToDo() {
        #expect(Failure.from(URLError(.notConnectedToInternet)).title == "No internet")
        #expect(Failure.from(URLError(.cannotConnectToHost)).action == .retry)
        #expect(Failure.from(URLError(.cannotFindHost)).action == .reconnect)
        #expect(Failure.from(URLError(.secureConnectionFailed)).title == "The connection isn't secure")
    }

    @Test func serverAnswersAreNeverShownRaw() {
        let raw = #"{"detail":{"problems":["points_file: missing"]}}"#
        for code in [400, 404, 409, 413, 422, 429, 500, 502, 503] {
            let f = Failure.from(GatewayError.http(code, raw))
            #expect(!f.message.contains("{") && !f.message.contains("points_file"), "\(code)")
            #expect(!f.title.isEmpty && !f.message.isEmpty)
        }
        #expect(Failure.from(GatewayError.http(401, "")).action == .reconnect)
        #expect(Failure.from(GatewayError.http(401, ""), context: .pairing).title == "That pairing code isn't right")
        #expect(Failure.from(GatewayError.http(503, raw)).title == "The server had a problem")
        #expect(Failure.from(GatewayError.http(409, "the design is unknown: name the product")).action == .enterCode)
    }

    @Test func fullStorageIsRecognisedFromTheFileError() {
        let e = NSError(domain: NSCocoaErrorDomain, code: NSFileWriteOutOfSpaceError)
        #expect(Failure.from(e).action == .freeSpace)
    }

    @Test func noMessageUsesAnExclamationMark() {
        let all = [Failure.storageFull, .cameraDenied, .bluetoothDenied, .notPaired, .reportTimeout,
                   .storageLow(freeBytes: 100 << 20, needed: 1 << 30), Failure.from(URLError(.timedOut)),
                   Failure.from(GatewayError.http(500, "")), Failure.from(CocoaError(.fileNoSuchFile))]
        for f in all { #expect(!(f.title + f.message).contains("!")) }
    }
}

/// Changing from one product to another (Settings > Change product code), and what that does to the server and the pairing.
@Suite(.serialized) @MainActor
struct ChangeProductTests {
    @Test func theNewProductReplacesTheOldAndGoesToTheServer() throws {
        UserDefaults.standard.removeObject(forKey: "gatewayURL")
        let app = AppState()
        defer { app.clearProduct(); app.unpair(); UserDefaults.standard.removeObject(forKey: "gatewayURL") }
        try app.use(try #require(ProductCode.parse("lamp-v2@lea.example.com")))
        #expect(app.product?.product == "lamp-v2")
        #expect(DesignID.current == "lamp-v2")
        try app.use(try #require(ProductCode.parse("fan-3@lea.example.com")))
        #expect(app.product?.product == "fan-3")
        #expect(DesignID.current == "fan-3")
        #expect(app.gatewayURL == "https://lea.example.com")
    }

    @Test func aNewServerUnpairsThePhoneAndASameServerDoesNot() throws {
        // A phone already paired to the first server, before the app state reads it.
        UserDefaults.standard.set("https://first.example.com", forKey: "gatewayURL")
        try Keychain.setToken("token-for-the-first-server")
        let app = AppState()
        defer { app.clearProduct(); app.unpair(); UserDefaults.standard.removeObject(forKey: "gatewayURL") }
        #expect(app.isPaired)

        // Another product on the same server keeps the pairing.
        try app.use(try #require(ProductCode.parse("lamp-v2@first.example.com")))
        #expect(app.isPaired && Keychain.token() == "token-for-the-first-server")

        // A product on another server drops it: that token belongs to the first server.
        try app.use(try #require(ProductCode.parse("fan-3@second.example.com")))
        #expect(!app.isPaired)
        #expect(Keychain.token() == nil)
        #expect(app.gatewayURL == "https://second.example.com")
    }
}
