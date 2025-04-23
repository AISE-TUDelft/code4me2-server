import React, { useState } from "react";
import "./Auth.css";
import Modal from "../common/Modal";
import { initGoogleOAuth } from "../../utils/auth";
import { authenticateUser } from "../../utils/api";
import { GoogleLogin } from "@react-oauth/google";

const Login = ({ onSwitchToSignup, onLogin, onGoogleAuth }) => {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [modal, setModal] = useState({
    isOpen: false,
    title: "",
    message: "",
    type: "info",
  });

  const showModal = (title, message, type) => {
    setModal({ isOpen: true, title, message, type });
  };

  const closeModal = () => {
    setModal({ ...modal, isOpen: false });
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setIsSubmitting(true);

    if (!email || !password) {
      setError("Please fill in all fields");
      setIsSubmitting(false);
      return;
    }

    try {
      console.log("Login attempt with:", { email });

      const response = await authenticateUser({ email, password });

      if (response.ok) {
        onLogin(response.user);
      } else {
        showModal("Login Failed", response.error, "error");
      }
    } catch (err) {
      setError("Login failed. Please try again.");
      console.error("Login error:", err);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="auth-container">
      <h2>Login</h2>
      {error && <div className="error-message">{error}</div>}

      {/* Success/Error Modal */}
      <Modal
        isOpen={modal.isOpen}
        onClose={closeModal}
        title={modal.title}
        message={modal.message}
        type={modal.type}
      />

      <form onSubmit={handleSubmit}>
        <div className="form-group">
          <label htmlFor="email">Email</label>
          <input
            type="email"
            id="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="Enter your email"
            required
            disabled={isSubmitting}
          />
        </div>

        <div className="form-group">
          <label htmlFor="password">Password</label>
          <input
            type="password"
            id="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Enter your password"
            required
            disabled={isSubmitting}
          />
        </div>

        <button type="submit" className="auth-button" disabled={isSubmitting}>
          {isSubmitting ? "Logging in..." : "Login"}
        </button>
      </form>

      <div className="auth-switch">
        Don't have an account?{" "}
        <button
          onClick={onSwitchToSignup}
          className="switch-button"
          disabled={isSubmitting}
        >
          Sign up
        </button>
      </div>

      <div className="oauth-section">
        <p>Or login with:</p>
        <GoogleLogin
          onSuccess={async (credentialResponse) => {
            const user = await initGoogleOAuth(credentialResponse);
            console.log("Google login successful:", user);
            onGoogleAuth(user);
          }}
          onError={() => {
            console.log("Google login failed");
            setError("Google login failed. Please try again.");
          }}
        />
      </div>
    </div>
  );
};

export default Login;
