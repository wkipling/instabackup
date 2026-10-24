// Disassemble and create a function at each address, then decompile it.
// Usage: -postScript ForceFunc.java <outDir> <addressListFile>
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.PrintWriter;

public class ForceFunc extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        File outDir = new File(args[0]);
        outDir.mkdirs();
        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);

        try (BufferedReader br = new BufferedReader(new FileReader(args[1]))) {
            String line;
            while ((line = br.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty() || line.startsWith("#")) continue;
                String[] parts = line.split("\\s+", 2);
                Address a = toAddr(parts[0]);
                String hint = parts.length > 1 ? parts[1] : parts[0];
                disassemble(a);
                Function f = getFunctionContaining(a);
                if (f == null) {
                    f = createFunction(a, hint);
                }
                if (f == null) { println("NOCREATE " + a); continue; }
                DecompileResults res = di.decompileFunction(f, 240, monitor);
                if (!res.decompileCompleted()) { println("FAIL " + a); continue; }
                File out = new File(outDir, String.format("%08x_%s.c", f.getEntryPoint().getOffset(),
                    hint.replaceAll("[^A-Za-z0-9_.-]", "_")));
                try (PrintWriter w = new PrintWriter(out)) {
                    w.println("// " + hint + " @ " + f.getEntryPoint() + " size=" + f.getBody().getNumAddresses());
                    w.println(res.getDecompiledFunction().getC());
                }
                println("WROTE " + out);
            }
        }
        di.dispose();
    }
}
