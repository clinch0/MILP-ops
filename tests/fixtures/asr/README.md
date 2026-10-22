# Short ASR confirmation regression

`short-identity-confirmation.wav` is the original failed synthetic Silero
`ru_ekaterina`, context stress, rate 1.5 recording from the isolated VM test.
It says «Да, данные верны», lasts 0.935875 s at 8 kHz PCM16 mono and contains
no patient data. SHA-256:
`03ca4e38b6515731fd085cb3472f0bda9b5800f673baeea23acc592caa17833e`.

Unpadded GigaAM v3 e2e RNN-T CPU int8 returned an empty transcript. With
200 ms zero context at each endpoint the same frozen bytes are recognized.
Do not regenerate this WAV to make the regression pass.
