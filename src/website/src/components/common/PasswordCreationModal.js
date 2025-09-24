import React, { useState } from "react";
import "./Modal.css";

/**
 * Modal for creating a password when signing up with Google
 *
 * @param {Object} props - Component props
 * @param {boolean} props.isOpen - Whether the modal is open
 * @param {Function} props.onClose - Function to call when the modal is closed
 * @param {Object} props.googleUser - User data from Google
 * @param {Function} props.onSubmit - Function to call when the form is submitted
 */
const PasswordCreationModal = ({ isOpen, onClose, googleUser, onSubmit }) => {
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  if (!isOpen) return null;

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setIsSubmitting(true);

    // Validate passwords
    if (!password || !confirmPassword) {
      setError("Please fill in all fields");
      setIsSubmitting(false);
      return;
    }

    if (password !== confirmPassword) {
      setError("Passwords do not match");
      setIsSubmitting(false);
      return;
    }
    // Check if password is strong enough
    const isStrongPassword = (password) => {
      const strongPasswordPattern =
        /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$/;
      return strongPasswordPattern.test(password);
    };
    if (!isStrongPassword(password)) {
      setError(
        "Password must contain at least 8 characters, including uppercase, lowercase letters and numbers",
      );
      setIsSubmitting(false);
      return;
    }

    try {
      // Call the onSubmit callback with the password
      await onSubmit(password);
      // Reset form
      setPassword("");
      setConfirmPassword("");
    } catch (err) {
      setError(err.message || "Failed to create account. Please try again.");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-container" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header info">
          <h3>Create Password</h3>
          <button className="modal-close" onClick={onClose}>
            ×
          </button>
        </div>
        <div className="modal-body">
          <p>
            Welcome, {googleUser.name || "User"}! To complete your account
            setup, please create a password for your account.
          </p>

          {error && <div className="error-message">{error}</div>}

          <form onSubmit={handleSubmit} className="password-form">
            <div className="form-group">
              <label htmlFor="password">Password</label>
              <input
                type="password"
                id="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Create a password"
                required
                // disabled={isSubmitting}
              />
            </div>

            <div className="form-group">
              <label htmlFor="confirmPassword">Confirm Password</label>
              <input
                type="password"
                id="confirmPassword"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder="Confirm your password"
                required
                disabled={isSubmitting}
              />
            </div>
          </form>
        </div>
        <div className="modal-footer">
          <button
            className="modal-button secondary"
            onClick={onClose}
            disabled={isSubmitting}
          >
            Cancel
          </button>
          <button
            className="modal-button info"
            onClick={handleSubmit}
            disabled={isSubmitting}
          >
            {isSubmitting ? "Creating Account..." : "Create Account"}
          </button>
        </div>
      </div>
    </div>
  );
};

export default PasswordCreationModal;
