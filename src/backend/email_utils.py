import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from App import App


def send_verification_email(user_email: str, user_name: str, verification_token: str):
    """
    Send a verification email to the user
    """
    config = App.get_instance().get_config()

    # Create message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Verify Your Email Address"
    msg["From"] = config.email_from
    msg["To"] = user_email

    # Create HTML version of the message
    verification_url = f"{config.verification_url}?token={verification_token}"
    html = f"""
    <html>
      <head></head>
      <body>
        <p>Hi {user_name},</p>
        <p>Thank you for signing up! Please verify your email address by clicking the button below:</p>
        <p>
          <a href="{verification_url}" style="display: inline-block; padding: 10px 20px; background-color: #4CAF50; color: white; text-decoration: none; border-radius: 5px;">
            Verify Email
          </a>
        </p>
        <p>If the button doesn't work, you can also copy and paste this link into your browser:</p>
        <p>{verification_url}</p>
        <p>This link will expire in 24 hours.</p>
        <p>Best regards,<br>The Code4Me Team</p>
      </body>
    </html>
    """

    # Attach HTML part
    msg.attach(MIMEText(html, "html"))

    try:
        # Connect to SMTP server
        server = smtplib.SMTP(config.email_host, config.email_port)
        if config.email_use_tls:
            server.starttls()
        server.login(config.email_username, config.email_password)

        # Send email
        server.sendmail(config.email_from, user_email, msg.as_string())
        server.quit()

        logging.info(f"Verification email sent to {user_email}")
        return True
    except Exception as e:
        logging.error(f"Failed to send verification email: {str(e)}")
        return False
