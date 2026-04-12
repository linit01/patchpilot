import SwiftUI

/// First-launch screen: enter PatchPilot server URL
struct ServerConfigView: View {
    @EnvironmentObject var apiClient: APIClient
    @State private var serverURL = ""
    @State private var isChecking = false
    @State private var errorMessage: String?
    @State private var isValid = false

    var body: some View {
        VStack(spacing: 24) {
            Spacer()

            Image(systemName: "server.rack")
                .font(.system(size: 60))
                .foregroundColor(Theme.cyan)

            Text("PatchPilot")
                .font(.largeTitle)
                .fontWeight(.bold)
                .foregroundColor(Theme.textPrimary)

            Text("Enter your PatchPilot server URL")
                .font(.subheadline)
                .foregroundColor(Theme.textSecondary)

            VStack(spacing: 12) {
                TextField("https://patchpilot.example.com:8080", text: $serverURL)
                    .textFieldStyle(.roundedBorder)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)

                if let error = errorMessage {
                    Text(error)
                        .font(.caption)
                        .foregroundColor(Theme.red)
                }

                if !serverURL.hasPrefix("https://") && !serverURL.isEmpty {
                    Label("Using HTTP is not recommended for production", systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundColor(Theme.amber)
                }
            }
            .padding(.horizontal, 32)

            Button(action: checkConnection) {
                HStack {
                    if isChecking {
                        ProgressView()
                            .tint(.white)
                    }
                    Text(isChecking ? "Connecting..." : "Connect")
                        .fontWeight(.semibold)
                }
                .frame(maxWidth: .infinity)
                .padding()
                .background(serverURL.isEmpty ? Theme.textMuted : Theme.cyan)
                .foregroundColor(.white)
                .cornerRadius(10)
            }
            .disabled(serverURL.isEmpty || isChecking)
            .padding(.horizontal, 32)

            Spacer()
            Spacer()
        }
        .background(Theme.bgBlack)
    }

    private func checkConnection() {
        errorMessage = nil
        isChecking = true

        apiClient.configure(serverURL: serverURL)

        Task {
            do {
                let reachable = try await apiClient.checkConnectivity()
                isChecking = false
                if reachable {
                    isValid = true
                } else {
                    errorMessage = "Server responded but setup status check failed"
                }
            } catch {
                isChecking = false
                errorMessage = "Cannot connect: \(error.localizedDescription)"
            }
        }
    }
}
