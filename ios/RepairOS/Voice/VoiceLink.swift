import Foundation

/// The gateway's side of a conversation: a short-lived Cartesia grant, and the reply to one final spoken turn.
struct VoiceLink: Sendable {
    var token: @Sendable () async throws -> CartesiaGrant
    var turn: @Sendable (VoiceTurnRequest) async throws -> VoiceReply
}

/// One final turn: what was said, which conversation it belongs to, and what the phone showed at that moment.
struct VoiceTurnRequest: Sendable {
    let id: String
    let mode: String
    let transcript: String
    let conversationID: String
    var sessionID: String?
    var live: [String: String] = [:]
}

/// What the open screen shows Lea right now: its gateway session (nil until it has one) and a few facts, such as the
/// scan's stage, the on-screen cue and progress, so Lea can talk someone through a scan while it runs.
struct VoiceScene: Sendable {
    var sessionID: String?
    var live: [String: String]
}

struct VoiceReply: Decodable, Sendable {
    let reply: String
    let actions: [VoiceAction]
}

/// Something the gateway asked the phone to do: `offer_scan` (show a scan button), `scan` (start a new scan now) or
/// `report` (open a scan's report). A scan carries the symptom from the conversation, so the scan starts with it.
struct VoiceAction: Decodable, Sendable, Equatable {
    let type: String
    var scanID: String?
    var symptom: String?
    enum CodingKeys: String, CodingKey { case type, symptom, scanID = "scan_id" }
}

struct CartesiaGrant: Decodable, Sendable {
    let token: String
    let expiresInSeconds: Int
    let version: String
    let voiceId: String
}
