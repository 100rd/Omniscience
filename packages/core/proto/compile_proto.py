import sys
import subprocess

def main():
    print("Installing grpcio-tools to compile protobuf...")
    # Since grpcio-tools is a build-time dependency for schema compilation, we make sure it is present
    subprocess.run([sys.executable, "-m", "pip", "install", "grpcio-tools"], check=True)

    print("Compiling proto file...")
    from grpc_tools import protoc
    protoc.main([
        "",
        "-Ipackages/core/proto",
        "--python_out=packages/core/src/omniscience_core/queue",
        "packages/core/proto/events.proto"
    ])
    print("Protobuf compilation completed successfully!")

if __name__ == "__main__":
    main()
