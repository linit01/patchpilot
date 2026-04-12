import SwiftUI

@main
struct PatchPilotApp: App {
    @StateObject private var apiClient = APIClient.shared
    @StateObject private var authService = AuthService.shared

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(apiClient)
                .environmentObject(authService)
        }
    }
}
