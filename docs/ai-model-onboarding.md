# On-premises AI model onboarding

PacketSafari keeps the Codex Agent engine and connects it to a
customer-controlled OpenAI-compatible Responses endpoint. Model discovery is
not certification. Each exact model, endpoint, inference-server revision, and
parser profile must pass the small PacketSafari synthetic qualification canary
before it can run Agent verification or final-report stages.

The complete cross-deployment contract and JSON example ship in the application
documentation at `documentation/deployment/ai-model-onboarding.md`. The
on-premises operator sequence is:

1. Start vLLM, SGLang, Ollama, LM Studio, or another compatible runtime outside
   PacketSafari. Follow the parser matrix for the exact installed release.
2. Make the endpoint reachable from both the PacketSafari backend and worker
   containers. Do not use `127.0.0.1` unless the inference server is in the same
   container. Prefer a private service DNS name or a routable private address.
3. Approve that destination under the on-premises AI egress policy.
4. Open **Admin → AI settings**, select **Other provider**, and enter the API
   root, normally ending in `/v1`.
5. Test the connection, refresh models, and use **Manual model onboarding** to
   add or import a schema-version `1` profile. Profiles contain no credentials.
6. Map the model roles, save, and explicitly select **Qualify selected model**.
   The operation makes three synthetic calls and sends no customer PCAP.
7. Confirm the model is `validated` and has the required `eligibleStages`
   before enabling it for production investigations.

For an air-gapped deployment, transfer the exported profile JSON with the
normal approved removable-media process. The model endpoint can be entirely
local. Qualification also runs locally; no public provider is required.

Current vLLM examples use automatic tool choice plus a model-specific parser,
such as `qwen3_xml` for Qwen3 Coder, `glm47` for GLM 4.7, or `kimi_k2` for Kimi
K2. These names can change between inference-server releases. SGLang uses its
own supported parser matrix and always receives a separate qualification
record. OpenRouter test results never certify the on-premises route.

PacketSafari can test profile import/export, routing, Responses streaming
fixtures, reasoning adaptation, drift, and fail-closed policy without GPU
hardware. Only the final synthetic canary needs a real endpoint. A passed mock
test must never be described as runtime validation.
