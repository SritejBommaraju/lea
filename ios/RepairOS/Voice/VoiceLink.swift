import Foundation

/// The gateway's side of a conversation: a short-lived Cartesia grant, and the reply to one final spoken turn.
struct VoiceLink: Sendable {
    var token: @Sendable () async throws -> CartesiaGrant
    var turn: @Sendable (_ id: String, _ mode: String, _ transcript: String) async throws -> VoiceReply
}

struct VoiceReply: Decodable, Sendable {
    let reply: String
    let actions: [VoiceAction]
}

/// Something the gateway asked the phone to do: `offer_scan` (show a scan button), `scan` (start a new scan now) or
/// `report` (open a scan's report).
struct VoiceAction: Decodable, Sendable, Equatable {
    let type: String
    let scanID: String?
    enum CodingKeys: String, CodingKey { case type, scanID = "scan_id" }
}

struct CartesiaGrant: Decodable, Sendable {
    let token: String
    let expiresInSeconds: Int
    let version: String
    let voiceId: String
}
