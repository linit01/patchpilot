import SwiftUI

/// Login screen with username/password
struct LoginView: View {
    @EnvironmentObject var authService: AuthService
    @State private var username = ""
    @State private var password = ""
    @State private var errorMessage: String?
    @State private var showServerConfig = false

    var body: some View {
        VStack(spacing: 24) {
            Spacer()

            Image(systemName: "shield.checkered")
                .font(.system(size: 50))
                .foregroundColor(Theme.cyan)

            Text("PatchPilot")
                .font(.largeTitle)
                .fontWeight(.bold)
                .foregroundColor(Theme.textPrimary)

            if let serverURL = KeychainHelper.serverURL {
                Text(serverURL)
                    .font(.caption)
                    .foregroundColor(Theme.textMuted)
            }

            VStack(spacing: 14) {
                TextField("Username", text: $username)
                    .textFieldStyle(.roundedBorder)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()

                SecureField("Password", text: $password)
                    .textFieldStyle(.roundedBorder)

                if let error = errorMessage {
                    Text(error)
                        .font(.caption)
                        .foregroundColor(Theme.red)
                        .multilineTextAlignment(.center)
                }
            }
            .padding(.horizontal, 32)

            Button(action: login) {
                HStack {
                    if authService.isLoading {
                        ProgressView()
                            .tint(.white)
                    }
                    Text(authService.isLoading ? "Signing in..." : "Sign In")
                        .fontWeight(.semibold)
                }
                .frame(maxWidth: .infinity)
                .padding()
                .background(canLogin ? Theme.cyan : Theme.textMuted)
                .foregroundColor(.white)
                .cornerRadius(10)
            }
            .disabled(!canLogin)
            .padding(.horizontal, 32)

            Button("Change Server") {
                showServerConfig = true
            }
            .font(.caption)
            .foregroundColor(Theme.textSecondary)

            Spacer()
            Spacer()
        }
        .background(Theme.bgBlack)
        .sheet(isPresented: $showServerConfig) {
            ServerConfigView()
        }
    }

    private var canLogin: Bool {
        !username.isEmpty && !password.isEmpty && !authService.isLoading
    }

    private func login() {
        errorMessage = nil
        Task {
            do {
                try await authService.login(username: username, password: password)
            } catch let error as APIError {
                errorMessage = error.errorDescription
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}
