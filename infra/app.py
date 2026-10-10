import aws_cdk as cdk
from stack import NagukChatbotStack

app = cdk.App()
NagukChatbotStack(app, "NagukChatbotStack")
app.synth()
